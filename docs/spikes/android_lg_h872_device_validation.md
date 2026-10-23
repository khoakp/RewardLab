---
last_updated: 2026-10-22
scope: device_validation
milestone: Personal-P3.1
device: LG-H872
status: accepted
verdict: CONDITIONAL
---

# Physical Device Validation: LG-H872 Android WebView Worker (Personal P3.1)

## 1. Executive Summary & Verification Context

- **Milestone:** Personal P3.1 — LG-H872 Android WebView Build & Device Validation
- **Device Model:** LG-H872 (T-Mobile LG G6)
- **Primary Objective:** Convert the Milestone Personal P3 WebView proof-of-concept into a fully structured, buildable Android Gradle project (`minSdk = 26`), implement mandatory architectural and security corrections, and validate physical device parameters via ADB on the Windows host.
- **Hardware Status:** The physical LG-H872 device was actively connected to the development environment via USB and inspected through `adb`. Physical device properties and network connectivity to RewardLab over `adb reverse` were empirically confirmed.

---

## 2. Empirically Verified Device Profile (from Live ADB Query)

| Parameter | Observed Value | Verification Command | Notes |
|---|---|---|---|
| **Device Model** | `LG-H872` | `adb shell getprop ro.product.model` | Confirmed physical hardware |
| **Android Version** | `8.0.0` (Oreo) | `adb shell getprop ro.build.version.release` | Target OS |
| **API Level** | `26` | `adb shell getprop ro.build.version.sdk` | `minSdk = 26` baseline |
| **CPU Architecture** | `arm64-v8a, armeabi-v7a, armeabi` | `adb shell getprop ro.product.cpu.abilist` | Qualcomm Snapdragon 821 (MSM8996 Pro) |
| **Total RAM** | `3777 MB` (~4 GB) | `adb shell free -m` | Physical memory pool |
| **Free + Buffers** | `479 MB` | `adb shell free -m` | Available memory under current load |
| **Swap Total/Used**| `1416 MB` / `591 MB` | `adb shell free -m` | ZRAM active |
| **Google WebView** | `133.0.6943.49` | `dumpsys package com.google.android.webview` | Modern Chromium engine running on Android 8 |
| **AOSP WebView** | `66.0.3359.126` | `dumpsys package com.android.webview` | Legacy fallback (inactive) |
| **Reverse Port Forwarding** | `tcp:8001 -> tcp:8001` | `adb reverse tcp:8001 tcp:8001` | **Verified**: allows phone to query local RewardLab via `127.0.0.1:8001` |
| **`/ui-fixtures` Connectivity** | **HTTP 200 OK** | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/ui-fixtures` | Confirmed from phone shell |
| **`/tasks/task_01` Auth Check** | **HTTP 302 Found** | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/tasks/task_01` | Confirmed: redirects to `/login` |
| **`/login` Connectivity** | **HTTP 200 OK** | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/login` | Confirmed from phone shell |

---

## 3. Mandatory Corrections Implemented (Phase A)

### 3.1 Buildable Android Gradle Project (`android/`)
1. **Root & Module Gradle:**
   - `android/settings.gradle.kts`: Configured with Google Maven, Maven Central, Gradle Plugin Portal.
   - `android/build.gradle.kts`: Root Gradle configuration with Android Gradle Plugin 8.2.2 and Kotlin 1.9.22.
   - `android/app/build.gradle.kts`: Configured with `minSdk = 26`, `compileSdk = 34`, `targetSdk = 34`, Java 1.8 compatibility, and typed dependencies (`androidx.core:core-ktx:1.12.0`, `androidx.appcompat:appcompat:1.6.1`, `kotlinx-coroutines-android:1.7.3`).
2. **Gradle Wrapper:**
   - Wrapper configured with Gradle 8.4 (`gradle/wrapper/gradle-wrapper.properties` and `gradle-wrapper.jar`).
   - `gradlew` (Linux/macOS) and `gradlew.bat` (Windows).

### 3.2 Real WebView in View Hierarchy & Lifecycle Safety
1. **Visual UI Layout (`activity_worker.xml`):**
   - Header with device notification and target endpoint radio group (`LAN`, `Host Wi-Fi`, `ADB Reverse`, `Emulator`).
   - Action controls: `Test 1: Read-Only`, `Test 2: Mutation`, `Open Login Page`, and `Stop`.
   - Real-time step progress indicator and status log output TextView.
   - Embedded `WebView` surface actively rendered on the screen.
2. **Activity Lifecycle Hardening (`AndroidWorkerActivity.kt`):**
   - Coroutine cancellation on `onDestroy()` (`coroutineScope.cancel()`, `activeWorkflowJob?.cancel()`).
   - WebView resource teardown: `stopLoading()`, `clearHistory()`, `loadUrl("about:blank")`, `destroy()`.
   - Re-creation guard: `savedInstanceState.getBoolean("hasExecutedMutation")` prevents duplicate mutations upon device orientation changes.
   - Does not require unconstrained wake-locks or permanent screen-on flags for basic foreground verification.

### 3.3 Network Security & Allowlist Enforcement (`RewardLabNetworkConfig.kt`)
1. **Elimination of Hardcoded `10.0.2.2`:**
   - Supports configurable targets:
     - Physical LAN: `http://192.168.1.122:8001`
     - Host Wi-Fi: `http://172.16.1.2:8001`
     - ADB Reverse: `http://127.0.0.1:8001`
     - Android Emulator: `http://10.0.2.2:8001`
2. **Deny-by-Default Validation:**
   - Pre-flight validation enforces permitted schemes (`http`, `https`), permitted ports (`8001`, `80`, `443`), and approved private IP addresses (`192.168.x.x`, `172.16-31.x.x`, `10.x.x.x`, `127.0.0.1`, `localhost`).
   - Strictly rejects public internet addresses, scheme-relative URLs (`//attacker.com`), path traversal (`..`), and embedded credentials (`user:pass@host`).
3. **Scoped Cleartext Configuration (`network_security_config.xml`):**
   - Global `cleartextTrafficPermitted="false"`.
   - Cleartext is allowed strictly for explicitly designated local test endpoints.

### 3.4 Fixture Correctness & Synthetic Authentication
1. **Accurate Task Target:**
   - Uses real synthetic task fixture `task_01` (avoiding nonexistent `task_1`).
   - Selectors match `task_detail.html`:
     - `data-testid="mark-reviewed-btn"`
     - `data-testid="task-status"` (`Pending` -> `Reviewed`)
     - `data-testid="review-success-alert"`
2. **Synthetic Authentication Flow:**
   - Direct manual login through the embedded WebView via the "Open Login Page" button.
   - Operator signs in using synthetic credentials (`test_01` / `test_password_01`).
   - Zero hardcoded passwords, zero secret tokens emitted to logs.

### 3.5 Exact Postcondition Parsing & Ambiguous Mutation Safety
1. **Structured JSON DOM Evaluation:**
   - Injected JavaScript returns an atomic JSON snapshot:
     ```javascript
     JSON.stringify({
         found: true,
         statusText: statusText,
         hasSuccessAlert: hasSuccessAlert,
         buttonDisabled: buttonDisabled,
         isPending: statusText === 'Pending',
         isReviewed: statusText === 'Reviewed'
     })
     ```
   - Replaces raw substring containment with strict JSON parsing via `JSONObject`.
2. **No Blind Retries:**
   - If postcondition cannot be confirmed after a mutating click, the worker transitions to `OutcomeType.OUTCOME_UNCONFIRMED` and halts immediately.

---

## 4. Phase B: Two-Tier Test Architecture

```text
+-----------------------------------------------------------------------------------------+
| Test 1: Read-Only Fixture Inspection (Zero Side Effects)                                |
| 1. Navigate to /ui-fixtures                                                            |
| 2. Await dynamic element [data-testid="delayed-btn"] (renders at 350ms)                 |
| 3. Extract innerText and verify exact match: "Delayed Button Ready"                     |
| 4. Outcome: READ_ONLY_VERIFIED                                                          |
+-----------------------------------------------------------------------------------------+
| Test 2: Verified Causal Mutation (task_01)                                              |
| 1. Navigate to /tasks/task_01 (checks if auth session exists; prompts login if needed)  |
| 2. Verify precondition: [data-testid="task-status"] text == "Pending"                   |
| 3. Mutating action: click [data-testid="mark-reviewed-btn"]                             |
| 4. Causal postcondition: verify status == "Reviewed" and success alert exists          |
| 5. Outcome: STATE_VERIFIED (or OUTCOME_UNCONFIRMED on ambiguity, with zero retry)       |
+-----------------------------------------------------------------------------------------+
```

---

## 5. Physical Validation & Resource Measurement Instructions

### Step 1: Build the Debug APK on Windows
Prerequisites on Windows workstation:
- **JDK 17 or JDK 21** installed and `JAVA_HOME` configured.
- **Android SDK Platform API 34** and Build Tools installed.

Run in PowerShell:
```powershell
cd android
.\gradlew.bat assembleDebug
```
Output artifact: `android/app/build/outputs/apk/debug/app-debug.apk`

### Step 2: Install onto LG-H872
```powershell
adb devices
adb install -r android\app\build\outputs\apk\debug\app-debug.apk
```

### Step 3: Configure Reverse Port or LAN
If testing via USB ADB reverse:
```powershell
adb reverse tcp:8001 tcp:8001
```
If testing via Wi-Fi:
- Ensure phone is connected to Wi-Fi.
- In the app UI, select "Host (172.16.1.2:8001)" or "LAN (192.168.1.122:8001)".

### Step 4: Run Physical Tests & Collect Telemetry
1. Open the application on LG-H872.
2. Click **"Test 1: Read-Only"** -> Observe `/ui-fixtures` load and `READ_ONLY_VERIFIED`.
3. Click **"Open Login"** -> Enter `test_01` / `test_password_01` -> Sign in.
4. Click **"Test 2: Mutation"** -> Observe click and `STATE_VERIFIED`.
5. Capture resource measurements via ADB:
```powershell
# Peak RAM RSS
adb shell dumpsys meminfo org.adbap.android.worker.debug | Select-String "TOTAL"

# CPU utilization
adb shell top -n 1 -b | Select-String "org.adbap.android"

# Battery and thermal status
adb shell dumpsys battery
```
6. **Laptop-Off Independence Test:**
   - Start RewardLab on standalone LAN test server or host.
   - Unplug USB cable completely from LG-H872.
   - Trigger workflow execution directly from device screen.
   - Verify workflow runs to completion without PC controller.
