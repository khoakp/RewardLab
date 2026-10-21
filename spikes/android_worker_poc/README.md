# ADBAP Personal P3: Android Standalone Browser Worker Spike

This spike explores whether an Android smartphone can act as an independent, standalone browser automation execution node running workflows against the authorized RewardLab web application without requiring an active PC/laptop execution controller, without an ongoing ADB connection, and without root access.

---

## 1. Candidate Architectures & Findings Summary

| Candidate Option | Architecture Description | Standalone Feasibility | Root Required? | PC/ADB Required? | Assessment |
|---|---|---|---|---|---|
| **Option A** | **Termux + Local Chromium/Playwright** | Termux on Android ARM64 running Python and a local headless browser. | **UNSUPPORTED** | No | Official Playwright provides no Bionic/Android ARM64 driver. PRoot emulation incurs 3-5x syscall overhead, requires disabling OS sandboxing (`--no-sandbox`), and triggers Android 12+ Phantom Process Killer (`SIGKILL`). |
| **Option B** | **Native Android App + Owned WebView** | Lightweight Android APK hosting a native `android.webkit.WebView` controlled via coroutines and JS DOM evaluation. | **FEASIBLE (CONDITIONAL)** | No | **No** (Zero PC or ADB dependency after install). Full DOM and JS execution, clean session isolation via `CookieManager`. Limitation: Requires foreground activity (screen on); background execution throttled by Android OS. |
| **Option C** | **Externally Controlled Chrome Android (Playwright ADB)** | Desktop laptop running Playwright connecting to Chrome on Android via `adb forward` over USB/TCP. | **NOT_STANDALONE** | No | **Yes (Mandatory)**. PC is the execution controller. Unplugging cable or closing laptop immediately aborts execution. |

---

## 2. Minimal POC Structure

This spike directory contains:
- `android_adapter.py`: Typed Python execution adapter contract mapping ADBAP Workflow DSL v1 steps (`navigate`, `click`, `fill`, `wait_visible`, `verify_state`, `request_human_action`), maintaining explicit causal verification mappings, and enforcing secret-free outputs.
- `test_android_adapter.py`: Automated contract unit tests verifying verified/unverified outcome classification, safety pauses, and credential redaction.
- `webview_app_spec/`: Full Kotlin/Android native reference implementation for Option B:
  - `AndroidManifest.xml`: Android application manifest with network and isolated process permissions.
  - `AndroidWorkflowModels.kt`: Typed data models matching ADBAP DSL.
  - `RewardLabWebViewClient.kt`: Native WebView client enforcing provider domain/port allowlist, asynchronous page navigation, and DOM click/state verification via JavaScript injection.
  - `AndroidWorkerActivity.kt`: Standalone foreground Activity executing a verified RewardLab workflow without PC/ADB.

---

## 3. How to Reproduce on Android Emulator or Physical Device

### Prerequisites
- Android Studio with Android SDK API 29+ (Android 10+).
- Physical Android phone or Android Virtual Device (AVD).
- RewardLab running on accessible host (e.g. `http://10.0.2.2:8001` for emulator, or local LAN IP `http://192.168.x.x:8001` for physical device).

### Build & Run Steps
1. Create a minimal Android project in Android Studio targeting API 29+.
2. Copy files from `webview_app_spec/` into `app/src/main/`:
   - `AndroidManifest.xml` -> `app/src/main/AndroidManifest.xml`
   - `*.kt` -> `app/src/main/java/org/adbap/android/worker/`
3. Build and install APK:
   ```bash
   ./gradlew installDebug
   ```
4. Launch "ADBAP Android Standalone Worker" on the device.
5. **Disconnect USB Cable / Disable Wireless ADB.**
6. Observe standalone execution in the foreground:
   - Navigates to `/tasks/task_1`.
   - Clicks "Mark as reviewed".
   - Verifies postcondition `Reviewed`.
   - Displays `Outcome: STATE_VERIFIED`.

---

## 4. Hardware Verification Status
- In this CI/development environment, no physical Android hardware or running emulator was attached.
- Per Milestone P3 guidelines, hardware execution is formally recorded as:
  - Hardware Execution: **`NOT_RUN`**
  - Resource Consumption: **`NOT_MEASURED`**
  - Verdict: **`CONDITIONAL`**
