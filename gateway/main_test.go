package main

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strings"
	"sync"
	"testing"
	"time"
)

func TestAdmissionBoundsAndRelease(t *testing.T) {
	a := &admission{users: map[string]int{}, resources: map[string]bool{}}
	var releases []func()
	for i := 0; i < 6; i++ {
		leave, ok := a.enter("a", "", true)
		if !ok {
			t.Fatal("rejected below per-user cap")
		}
		releases = append(releases, leave)
	}
	if _, ok := a.enter("a", "", true); ok {
		t.Fatal("per-user cap bypassed")
	}
	for i := 0; i < 2; i++ {
		leave, ok := a.enter("b", "", true)
		if !ok {
			t.Fatal("global cap premature")
		}
		releases = append(releases, leave)
	}
	if _, ok := a.enter("c", "", true); ok {
		t.Fatal("global cap bypassed")
	}
	for _, f := range releases {
		f()
		f()
	}
	if a.uploads != 0 || len(a.users) != 0 {
		t.Fatal("slots leaked")
	}
	releases = nil
	for i := 0; i < 12; i++ {
		leave, ok := a.enter("", "", false)
		if !ok {
			t.Fatal("download cap premature")
		}
		releases = append(releases, leave)
	}
	if _, ok := a.enter("", "", false); ok {
		t.Fatal("download cap bypassed")
	}
	for _, f := range releases {
		f()
	}
}
func TestStreamingAndHeaders(t *testing.T) {
	payload := bytes.Repeat([]byte("unchanged-transfer-content"), 10000)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("X-Forwarded-User") != "astro" {
			t.Error("identity lost")
		}
		if r.Header.Get("Range") != "bytes=0-99" {
			t.Error("range lost")
		}
		body, err := io.ReadAll(r.Body)
		if err != nil {
			t.Error(err)
		}
		w.Header().Set("Content-Range", "bytes 0-99/100")
		w.WriteHeader(206)
		w.Write(body)
	}))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	r := httptest.NewRequest("POST", "/drive/api/resources/foo", bytes.NewReader(payload))
	r.Header.Set("X-Forwarded-User", "astro")
	r.Header.Set("Range", "bytes=0-99")
	w := httptest.NewRecorder()
	gateway(u).ServeHTTP(w, r)
	if w.Code != 206 || !bytes.Equal(w.Body.Bytes(), payload) || w.Header().Get("Content-Range") == "" {
		t.Fatal("stream content/status/headers changed")
	}
}
func TestValidationAndEscapedTusAlias(t *testing.T) {
	entered, unblock := make(chan struct{}, 1), make(chan struct{})
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { entered <- struct{}{}; <-unblock; w.WriteHeader(204) }))
	defer backend.Close()
	u, _ := url.Parse(backend.URL)
	g := gateway(u)
	for _, tc := range []struct {
		method, path, key, value string
		length                   int64
		want                     int
	}{
		{"POST", "/drive/api/resources/foo", "", "", 0, 401},
		{"POST", "/drive/api/tus/foo", "Upload-Length", "-1", 0, 400},
		{"POST", "/drive/api/tus/foo", "Upload-Length", "68719476737", 0, 413},
		{"POST", "/drive/api/resources/foo", "", "", -1, 411},
		{"PATCH", "/drive/api/tus/foo", "Content-Type", "application/offset+octet-stream", (16 << 20) + 1, 413},
	} {
		r := httptest.NewRequest(tc.method, tc.path, strings.NewReader(""))
		r.ContentLength = tc.length
		if tc.want != 401 {
			r.Header.Set("X-Forwarded-User", "astro")
		}
		r.Header.Set(tc.key, tc.value)
		w := httptest.NewRecorder()
		g.ServeHTTP(w, r)
		if w.Code != tc.want {
			t.Fatalf("%+v got %d", tc, w.Code)
		}
	}
	done := make(chan struct{})
	go func() {
		r := httptest.NewRequest("PATCH", "/drive/api/tus/foo", strings.NewReader("a"))
		r.Header.Set("X-Forwarded-User", "astro")
		r.Header.Set("Content-Type", "application/offset+octet-stream")
		g.ServeHTTP(httptest.NewRecorder(), r)
		close(done)
	}()
	<-entered
	r := httptest.NewRequest("PATCH", "/drive/api/tus/%66oo", strings.NewReader("b"))
	r.Header.Set("X-Forwarded-User", "family")
	r.Header.Set("Content-Type", "application/offset+octet-stream")
	w := httptest.NewRecorder()
	g.ServeHTTP(w, r)
	if w.Code != 429 {
		t.Errorf("escaped alias bypassed lock: %d", w.Code)
	}
	close(unblock)
	<-done
}

func TestCanceledTransferReleasesResource(t *testing.T) {
	first := make(chan struct{})
	cleanup := make(chan struct{})
	var once sync.Once
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		isFirst := false
		once.Do(func() { isFirst = true; close(first) })
		if isFirst {
			io.Copy(io.Discard, r.Body)
			select {
			case <-r.Context().Done():
			case <-cleanup:
			}
			return
		}
		io.Copy(io.Discard, r.Body)
		w.WriteHeader(204)
	}))
	defer backend.Close()
	defer close(cleanup)
	u, _ := url.Parse(backend.URL)
	front := httptest.NewServer(gateway(u))
	defer front.Close()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req, _ := http.NewRequestWithContext(ctx, "PATCH", front.URL+"/drive/api/tus/cancel", strings.NewReader("payload"))
	req.Header.Set("X-Forwarded-User", "astro")
	req.Header.Set("Content-Type", "application/offset+octet-stream")
	done := make(chan struct{})
	go func() {
		resp, _ := http.DefaultClient.Do(req)
		if resp != nil {
			resp.Body.Close()
		}
		close(done)
	}()
	select {
	case <-first:
	case <-time.After(2 * time.Second):
		t.Fatal("transfer never reached backend")
	}
	cancel()
	<-done
	deadline := time.Now().Add(2 * time.Second)
	for {
		req, _ = http.NewRequest("PATCH", front.URL+"/drive/api/tus/cancel", strings.NewReader("next"))
		req.Header.Set("X-Forwarded-User", "astro")
		req.Header.Set("Content-Type", "application/offset+octet-stream")
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode == 204 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("resource slot leaked after cancel: %d", resp.StatusCode)
		}
		time.Sleep(10 * time.Millisecond)
	}
}
