val proofRoot = File(System.getenv("MOBILE_SERVER_PROOF_ROOT")
    ?: error("MOBILE_SERVER_PROOF_ROOT must point to the maintained proof checkout"))
val candidatePath = System.getProperty("mobile.proof.jar")
    ?: System.getenv("MOBILE_PROOF_JAR")
    ?: File(proofRoot, ".work/packaged-candidate.jar").canonicalPath
val packagedRuntime = File(candidatePath)
val proofTmp = File(proofRoot, ".work/resource-proof-tmp")
val proofWorkspace = File(proofRoot, ".work/resource-proof-workspace")
val proofHome = File(proofRoot, ".work/resource-proof-home")
listOf(proofTmp, proofWorkspace, proofHome).forEach { it.mkdirs() }
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
            workingDir = proofWorkspace
            systemProperty("user.home", proofHome.canonicalPath)
            systemProperty("java.util.prefs.userRoot", File(proofHome, "prefs").canonicalPath)
            systemProperty("java.util.prefs.systemRoot", File(proofHome, "system-prefs").canonicalPath)
            jvmArgs("-Djava.io.tmpdir=" + proofTmp.canonicalPath)
            systemProperty("java.io.tmpdir", proofTmp.canonicalPath)
            systemProperty("mobile.proof.tmp", proofTmp.canonicalPath)
            systemProperty("mobile.proof.jar", packagedRuntime.canonicalPath)
            systemProperty("mobile.proof.root", proofRoot.canonicalPath)
            System.getProperty("mobile.proof.nonce")?.let {
                systemProperty("mobile.proof.nonce", it)
            }
            System.getProperty("mobile.proof.expected.sha")?.let {
                systemProperty("mobile.proof.expected.sha", it)
            }
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
        test.workingDir = proofWorkspace
        test.systemProperty("user.dir", proofWorkspace.canonicalPath)
        val original = test.classpath
        test.classpath = server.files(packagedRuntime, original)
    }
}
