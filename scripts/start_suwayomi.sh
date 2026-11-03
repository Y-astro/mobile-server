#!/data/data/com.termux/files/usr/bin/bash
set -eu

export PREFIX="/data/data/com.termux/files/usr"
export HOME="/data/data/com.termux/files/home"
export PATH="$PREFIX/bin:$PREFIX/bin/applets:$PATH"
export TMPDIR="$PREFIX/tmp"
cd "$HOME/suwayomi"

# Ensure Suwayomi 12:00 AM daily reading list scheduler daemon is active
if ! pgrep -f "suwayomi_daily_update.py --daemon" >/dev/null 2>&1; then
    /data/data/com.termux/files/usr/bin/python3 /data/data/com.termux/files/home/bin/suwayomi_daily_update.py --daemon >> /data/data/com.termux/files/home/suwayomi/data/logs/scheduler_daemon.log 2>&1 &
fi

# Ensure Suwayomi GraphQL transparent filter daemon is active
if ! pgrep -f "suwayomi_graphql_filter.py" >/dev/null 2>&1; then
    /data/data/com.termux/files/usr/bin/python3 /data/data/com.termux/files/home/bin/suwayomi_graphql_filter.py >> /data/data/com.termux/files/home/suwayomi/data/logs/graphql_filter.log 2>&1 &
fi

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

# Prefer native Termux OpenJDK 21; fall back to Debian PRoot if missing
if [ -x "$PREFIX/bin/java" ]; then
    exec "$PREFIX/bin/java" "${JVM_ARGS[@]}" -jar "$HOME/suwayomi/bin/Suwayomi-Server.jar"
else
    exec "$PREFIX/bin/proot-distro" login debian --bind /storage/emulated/0:/storage/emulated/0 -- \
        /usr/bin/env LANG=C.UTF-8 LC_ALL=C.UTF-8 /usr/bin/java \
        "${JVM_ARGS[@]}" \
        -jar "$HOME/suwayomi/bin/Suwayomi-Server.jar"
fi
