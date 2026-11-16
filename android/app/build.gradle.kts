plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "org.opscart.bleinput"
    compileSdk = 35

    defaultConfig {
        applicationId = "org.opscart.bleinput"
        minSdk = 26
        targetSdk = 35
        versionCode = 1
        versionName = "0.1"
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }
    testOptions {
        unitTests.all {
            // 和推车侧 tests/test_ble_input_core.py 读同一份测试向量
            it.systemProperty("bleVectors", rootProject.file("../docs/ble_input_vectors.json").absolutePath)
        }
    }
}

dependencies {
    testImplementation("junit:junit:4.13.2")
    testImplementation("org.json:json:20240303")    // 安卓自带的 org.json 在本地单元测试里是空壳
}
