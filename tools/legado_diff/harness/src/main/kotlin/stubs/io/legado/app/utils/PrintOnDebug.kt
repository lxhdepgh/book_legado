package io.legado.app.utils

// Stand-in for LogUtils.printOnDebug: the JVM harness has no Android logger.
fun Throwable.printOnDebug() {
    System.err.println("[printOnDebug] $this")
}
