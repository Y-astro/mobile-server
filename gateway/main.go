// mobile-gateway bounds active FileBrowser transfers without buffering files.
package main

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"path"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxFileBytes int64 = 64 << 30
const maxTusChunkBytes int64 = 16 << 20

type admission struct {
	mu                 sync.Mutex
	uploads, downloads int
	users              map[string]int
	resources          map[string]bool
}

func (a *admission) enter(user, resource string, upload bool) (func(), bool) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if upload {
		if a.uploads >= 8 || a.users[user] >= 6 || (resource != "" && a.resources[resource]) {
			return nil, false
		}
		a.uploads++
		a.users[user]++
		if resource != "" {
			a.resources[resource] = true
		}
	} else {
		if a.downloads >= 12 {
			return nil, false
		}
		a.downloads++
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			a.mu.Lock()
			defer a.mu.Unlock()
			if upload {
				a.uploads--
				a.users[user]--
				if a.users[user] == 0 {
					delete(a.users, user)
				}
				if resource != "" {
					delete(a.resources, resource)
				}
			} else {
				a.downloads--
			}
		})
	}, true
}

// Each read/write gets an idle deadline, not a fixed whole-file time limit.
type idleBody struct {
	io.ReadCloser
	response http.ResponseWriter
}

func (r *idleBody) Read(p []byte) (int, error) {
	_ = http.NewResponseController(r.response).SetReadDeadline(time.Now().Add(60 * time.Second))
	return r.ReadCloser.Read(p)
}

type idleWriter struct{ http.ResponseWriter }

func (w *idleWriter) Write(p []byte) (int, error) {
	_ = http.NewResponseController(w.ResponseWriter).SetWriteDeadline(time.Now().Add(60 * time.Second))
	return w.ResponseWriter.Write(p)
}
func (w *idleWriter) Unwrap() http.ResponseWriter { return w.ResponseWriter }
func (w *idleWriter) Flush() {
	_ = http.NewResponseController(w.ResponseWriter).Flush()
}

type bufferPool struct{ sync.Pool }

func (b *bufferPool) Get() []byte {
	if v := b.Pool.Get(); v != nil {
		return v.([]byte)
	}
	return make([]byte, 64*1024)
}
func (b *bufferPool) Put(p []byte) {
	if cap(p) == 64*1024 {
		b.Pool.Put(p[:64*1024])
	}
}

func gateway(target *url.URL) http.Handler {
	proxy := httputil.NewSingleHostReverseProxy(target)
	proxy.BufferPool = &bufferPool{}
	proxy.Transport = &http.Transport{
		Proxy:           nil,
		DialContext:     (&net.Dialer{Timeout: 5 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		MaxConnsPerHost: 32, MaxIdleConnsPerHost: 16, MaxIdleConns: 32,
		IdleConnTimeout: 60 * time.Second, ResponseHeaderTimeout: 60 * time.Second,
		DisableCompression: true,
	}
	proxy.ErrorHandler = func(w http.ResponseWriter, r *http.Request, err error) {
		if !errors.Is(err, context.Canceled) {
			log.Printf("FileBrowser upstream failure: %T", err)
		}
		http.Error(w, "Storage backend unavailable", http.StatusBadGateway)
	}
	a := &admission{users: map[string]int{}, resources: map[string]bool{}}
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/healthz" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = io.WriteString(w, `{"status":"ok"}`)
			return
		}
		user := r.Header.Get("X-Forwarded-User")
		if len(user) > 128 {
			http.Error(w, "Invalid user", 400)
			return
		}
		write := r.Method == "POST" || r.Method == "PATCH" || r.Method == "PUT" || r.Method == "DELETE"
		if write && user == "" {
			http.Error(w, "Authentication required", 401)
			return
		}
		if !strings.HasPrefix(r.URL.Path, "/drive/") {
			http.NotFound(w, r)
			return
		}
		if length := r.Header.Get("Upload-Length"); length != "" {
			n, err := strconv.ParseInt(length, 10, 64)
			if err != nil || n < 0 {
				http.Error(w, "Invalid Upload-Length", 400)
				return
			}
			if n > maxFileBytes {
				http.Error(w, "File exceeds 64 GiB limit", 413)
				return
			}
		}
		if offset := r.Header.Get("Upload-Offset"); offset != "" {
			n, err := strconv.ParseInt(offset, 10, 64)
			if err != nil || n < 0 || n > maxFileBytes {
				http.Error(w, "Invalid Upload-Offset", 400)
				return
			}
		}
		maxBody := maxFileBytes
		if r.Method == "PATCH" && strings.HasPrefix(r.Header.Get("Content-Type"), "application/offset+octet-stream") {
			maxBody = maxTusChunkBytes
		}
		if r.ContentLength > maxBody {
			http.Error(w, "Request body too large", 413)
			return
		}
		if write && r.ContentLength < 0 {
			http.Error(w, "Content-Length required", 411)
			return
		}
		if write && r.Header.Get("Upload-Defer-Length") != "" {
			http.Error(w, "Declare upload length before streaming", 400)
			return
		}
		resource := ""
		// Prevent concurrent writes to the same TUS resource, including across users.
		if r.Method == "PATCH" && strings.HasPrefix(r.Header.Get("Content-Type"), "application/offset+octet-stream") {
			resource = path.Clean(r.URL.Path)
		}
		leave, ok := a.enter(user, resource, write)
		if !ok {
			w.Header().Set("Retry-After", "2")
			http.Error(w, "Transfer limit reached; retry shortly", http.StatusTooManyRequests)
			return
		}
		defer leave()
		if write && r.Body != nil {
			r.Body = &idleBody{http.MaxBytesReader(w, r.Body, maxBody), w}
		}
		// The internal listener is loopback-only. Caddy supplies authenticated identity.
		proxy.ServeHTTP(&idleWriter{w}, r)
	})
}

type limitedListener struct {
	net.Listener
	slots chan struct{}
}
type limitedConn struct {
	net.Conn
	once    sync.Once
	release func()
}

func (c *limitedConn) Close() error { err := c.Conn.Close(); c.once.Do(c.release); return err }
func (l *limitedListener) Accept() (net.Conn, error) {
	l.slots <- struct{}{}
	c, err := l.Listener.Accept()
	if err != nil {
		<-l.slots
		return nil, err
	}
	return &limitedConn{Conn: c, release: func() { <-l.slots }}, nil
}

func main() {
	listen := flag.String("listen", "127.0.0.1:8081", "Loopback listener")
	backend := flag.String("backend", "http://127.0.0.1:8080", "Fixed FileBrowser backend")
	flag.Parse()
	u, err := url.Parse(*backend)
	if err != nil || u.Scheme != "http" || u.Host != "127.0.0.1:8080" {
		log.Fatal("Backend must be http://127.0.0.1:8080")
	}
	host, _, err := net.SplitHostPort(*listen)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		log.Fatal("Listener must be loopback-only")
	}
	l, err := net.Listen("tcp", *listen)
	if err != nil {
		log.Fatal(err)
	}
	server := &http.Server{Handler: gateway(u), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 32 * 1024}
	fmt.Println("FileBrowser transfer gateway ready on", *listen)
	log.Fatal(server.Serve(&limitedListener{Listener: l, slots: make(chan struct{}, 64)}))
}
