#!/data/data/com.termux/files/usr/bin/bash
set -eu

export PREFIX="/data/data/com.termux/files/usr"
export HOME="/data/data/com.termux/files/home"
export PATH="$PREFIX/bin:$PREFIX/bin/applets:$PATH"
export TMPDIR="$PREFIX/tmp"
cd "$HOME/suwayomi"

# Suwayomi filter and daily update scheduler are supervised separately by server_supervisor.

JVM_ARGS=(
    -XX:+UseSerialGC
    -XX:TieredStopAtLevel=1
    -XX:CICompilerCount=2
    -Xms64m
    -Xmx384m
    -Dh2.defaultCacheSize=65536
    -Dh2.maxMemoryRows=50000
    -Dfile.encoding=UTF-8
    -Dsun.jnu.encoding=UTF-8
    -Djava.awt.headless=true
    -Dsuwayomi.tachidesk.config.server.rootDir="$HOME/suwayomi/data"
)

# Intercept Android Bionic dnsproxyd via lightweight UDP DNS resolver
# Fixes getaddrinfo hang caused by Android netd / VPN routing
export LD_PRELOAD="$HOME/suwayomi/libdns_fix.so${LD_PRELOAD:+:$LD_PRELOAD}"

if [ -x "$PREFIX/bin/java" ]; then
    exec "$PREFIX/bin/java" "${JVM_ARGS[@]}" -jar "$HOME/suwayomi/bin/Suwayomi-Server.jar"
else
    exec "$PREFIX/bin/proot-distro" login debian --bind /storage/emulated/0:/storage/emulated/0 --work-dir "$HOME/suwayomi" -- \
        /usr/bin/env LANG=C.UTF-8 LC_ALL=C.UTF-8 /usr/bin/java \
        "${JVM_ARGS[@]}" \
        -jar "$HOME/suwayomi/bin/Suwayomi-Server.jar"
fi
