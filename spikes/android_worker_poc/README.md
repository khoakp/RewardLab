# ADBAP Personal P3 & P3.1: Android Standalone Browser Worker Spike

This spike explores whether an Android smartphone can act as an independent, standalone browser automation execution node running workflows against the authorized RewardLab web application without requiring an active PC/laptop execution controller, without an ongoing ADB connection, and without root access.

In **Milestone Personal P3.1**, the reference code was migrated into a fully buildable Android Gradle project located at `android/`, with `minSdk = 26` targeting physical hardware validation on the **LG-H872** (Android 8.0.0, API 26, Snapdragon 821, 4GB RAM).

---

## 1. Candidate Architectures & Findings Summary

| Candidate Option | Architecture Description | Standalone Feasibility | Root Required? | PC/ADB Required? | Assessment |
|---|---|---|---|---|---|
| **Option A** | **Termux + Local Chromium/Playwright** | Termux on Android ARM64 running Python and a local headless browser. | **UNSUPPORTED** | No | Official Playwright provides no Bionic/Android ARM64 driver. PRoot emulation incurs 3-5x syscall overhead, requires disabling OS sandboxing (`--no-sandbox`), and triggers Android 12+ Phantom Process Killer (`SIGKILL`). |
| **Option B** | **Native Android App + Owned WebView** | Lightweight Android APK hosting a native `android.webkit.WebView` controlled via coroutines and JS DOM evaluation. | **FEASIBLE (CONDITIONAL)** | No | **No** (Zero PC or ADB dependency after install). Full DOM and JS execution, clean session isolation via `CookieManager`. Limitation: Requires foreground activity (screen on); background execution throttled by Android OS. |
| **Option C** | **Externally Controlled Chrome Android (Playwright ADB)** | Desktop laptop running Playwright connecting to Chrome on Android via `adb forward` over USB/TCP. | **NOT_STANDALONE** | No | **Yes (Mandatory)**. PC is the execution controller. Unplugging cable or closing laptop immediately aborts execution. |

---

## 2. Project Structure & Codebase Locations

1. **`android/` (Buildable Android Gradle Project):**
   - `settings.gradle.kts`
   - `build.gradle.kts`
   - `app/build.gradle.kts` (`minSdk = 26`, `compileSdk = 34`, AGP 8.2.2, Kotlin 1.9.22)
   - `app/src/main/AndroidManifest.xml` (scoped network permissions, network security config)
   - `app/src/main/res/xml/network_security_config.xml` (deny-by-default, cleartext allowed only for test targets)
   - `app/src/main/res/layout/activity_worker.xml` (live WebView surface, status card, Start/Stop buttons)
   - `app/src/main/java/org/adbap/android/worker/`:
     - `RewardLabNetworkConfig.kt`: Deny-by-default URL validation, port/scheme checking.
     - `RewardLabWebViewClient.kt`: Strict allowlist origin filtering, coroutine navigation, structured DOM inspection.
     - `AndroidWorkerActivity.kt`: Full Activity lifecycle, coroutine cancellation, recreation guards, two-tier testing.
     - `AndroidWorkflowModels.kt`: Typed outcome models (`STATE_VERIFIED`, `OUTCOME_UNCONFIRMED`, `READ_ONLY_VERIFIED`).
   - `app/src/test/java/org/adbap/android/worker/AndroidWorkerUnitTest.kt`: Unit test suite.

2. **`spikes/android_worker_poc/` (Spike & Python Bridge Contracts):**
   - `android_adapter.py`: Typed Python execution adapter contract mapping ADBAP Workflow DSL v1 steps.
   - `test_android_adapter.py`: Automated contract unit tests verifying verified/unverified outcome classification.
   - `webview_app_spec/`: Synced Kotlin spec files.

---

## 3. How to Build & Validate on Windows

### Prerequisites
- JDK 17 or JDK 21 (required by Android Gradle Plugin 8.2+).
- Android SDK with Platform API 34 and Build Tools.
- `adb` in PATH.

### Building Debug APK
```powershell
cd android
.\gradlew.bat assembleDebug
```
The resulting debug APK is located at:
`android/app/build/outputs/apk/debug/app-debug.apk`

### Installing & Validating on Physical LG-H872
```powershell
# 1. Verify device is connected
adb devices
# Expected: LGH872... device

# 2. Check API level and architecture
adb shell getprop ro.build.version.sdk      # Expected: 26
adb shell getprop ro.product.cpu.abilist     # Expected: arm64-v8a,armeabi-v7a,armeabi

# 3. Install APK
adb install -r android\app\build\outputs\apk\debug\app-debug.apk

# 4. Forward local RewardLab port if using ADB reverse (or use Wi-Fi LAN IP)
adb reverse tcp:8001 tcp:8001

# 5. Monitor logs
adb logcat -s ADBAP_WorkerActivity ADBAP_WebViewClient
```

---

## 4. Personal P3.1 Physical Hardware Status

- **Physical Device:** LG-H872 (Android 8.0.0, API 26, Snapdragon 821, 4 GB RAM).
- **Android System WebView:** 133.0.6943.49.
- **Development Host State:** Windows host has Oracle JRE 8 and ADB, but lacks JDK 17+ and Android SDK CLI tools; assembleDebug could not be built directly inside the agent shell. Full Gradle project and build instructions are provided for compilation.
- **Hardware Execution:** **`NOT_RUN`** (pending compilation on machine with Android SDK).
- **Gate Verdict:** **`PERSONAL-P3.1 CONDITIONAL`**.
