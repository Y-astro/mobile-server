allprojects {
    tasks.withType<org.gradle.api.tasks.testing.Test>().configureEach {
        maxHeapSize = "384m"
        minHeapSize = "64m"
        jvmArgs("-XX:+UseSerialGC", "-XX:TieredStopAtLevel=1", "-XX:CICompilerCount=2",
                "-Dh2.defaultCacheSize=65536", "-Dh2.maxMemoryRows=50000",
                "-Dfile.encoding=UTF-8", "-Dsun.jnu.encoding=UTF-8", "-Djava.awt.headless=true")
    }
}
