plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
    id("com.chaquo.python")
}

android {
    namespace = "com.example.legodo_client"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        // TODO: Specify your own unique Application ID (https://developer.android.com/studio/build/application-id.html).
        applicationId = "com.example.legodo_client"
        // You can update the following values to match your application needs.
        // For more information, see: https://flutter.dev/to/review-gradle-config.
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
        
        // Chaquopy Python configuration
        ndk {
            abiFilters += listOf("arm64-v8a", "armeabi-v7a", "x86_64")
        }
    }

    buildTypes {
        release {
            // TODO: Add your own signing config for the release build.
            // Signing with the debug keys for now, so `flutter run --release` works.
            signingConfig = signingConfigs.getByName("debug")
        }
    }
}

// Chaquopy configuration
chaquopy {
    defaultConfig {
        // Python packages to install via pip
        pip {
            install("aiohttp>=3.9.0")
            install("beautifulsoup4>=4.12.0")
            install("charset-normalizer>=3.0.0")
            install("html5lib>=1.1")
            install("elementpath>=5.0.0")
            install("jsonpath-ng>=1.7.0")
            install("opencc-python-reimplemented>=0.1.7")
            install("regex>=2024.0.0")
            install("fonttools>=4.55")
            install("pillow>=10.4")
            install("pycryptodome>=3.20")
            install("quickjs>=1.19.4")
            install("py7zr>=0.22")
            install("rarfile>=4.2")
            install("EbookLib>=0.18")
            install("pymupdf>=1.24")
            install("feedparser>=6.0.10")
            install("pyttsx3>=2.90")
            install("edge-tts>=6.1")
            install("qrcode>=7.4")
            install("opencv-python-headless>=4.8")
        }
        
        // Copy Python source files
        srcDirs("../../app")
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
