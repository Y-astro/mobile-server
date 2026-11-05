val proofRoot = File(System.getenv("MOBILE_SERVER_PROOF_ROOT")
    ?: error("MOBILE_SERVER_PROOF_ROOT must point to the maintained proof checkout"))
val packagedRuntime = File(proofRoot, ".work/packaged-candidate.jar")
allprojects {
    if (name == "server") {
        pluginManager.withPlugin("java") {
            extensions.configure<org.gradle.api.plugins.JavaPluginExtension> {
                sourceSets.named("test") {
                    java.srcDir(File(proofRoot, "suwayomi/parallel-downloads/verification/java"))
                    java.srcDir(File(proofRoot, "suwayomi/parallel-downloads/verification/kotlin"))
                }
            }
        }
        tasks.withType<org.gradle.api.tasks.testing.Test>().configureEach {
            systemProperty("mobile.proof.jar", packagedRuntime.canonicalPath)
            systemProperty("mobile.proof.root", proofRoot.canonicalPath)
            testLogging.showStandardStreams = false
            doFirst {
                require(classpath.files.size > 1) { "Expected JUnit and the compiled tests" }
                require(classpath.files.first().canonicalFile == packagedRuntime.canonicalFile)
                File(proofRoot, ".superpowers/sdd/packaged-test-classpath.txt").writeText(
                    classpath.files.joinToString("\n") { it.canonicalPath } + "\n")
            }
        }
    }
}

// Realize the test task after its JVM test-suite conventions are configured.
// Capturing classpath earlier would replace the not-yet-bound default with only
// the candidate and silently discard JUnit and the compiled test classes.
gradle.projectsEvaluated {
    rootProject.findProject(":server")?.let { server ->
        val test = server.tasks.named<org.gradle.api.tasks.testing.Test>("test").get()
        val original = test.classpath
        test.classpath = server.files(packagedRuntime, original)
    }
}
