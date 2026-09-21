plugins {
    kotlin("jvm") version "2.3.10"
    application
}

repositories {
    mavenCentral()
}

dependencies {
    implementation("org.jsoup:jsoup:1.16.2")
    implementation("com.jayway.jsonpath:json-path:2.10.0")
    implementation("cn.wanghaomiao:JsoupXpath:2.5.3")
    implementation("net.minidev:json-smart:2.5.2")
}

application {
    mainClass.set("DiffMainKt")
}

kotlin {
    jvmToolchain(17)
}
