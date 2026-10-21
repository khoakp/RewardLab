package org.adbap.android.worker

import android.app.Activity
import android.os.Bundle
import android.util.Log
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.util.UUID

/**
 * AndroidWorkerActivity: Hosts the standalone WebView automation execution.
 * Demonstrates:
 * - 100% Standalone execution on Android hardware
 * - Zero dependency on laptop/PC execution controller
 * - Zero dependency on ADB host during execution
 * - Clean session cookie injection and outcome verification
 */
class AndroidWorkerActivity : Activity() {

    private val tag = "ADBAP_AndroidWorker"
    private lateinit var webView: WebView
    private lateinit var statusText: TextView
    private val scope = CoroutineScope(Dispatchers.Main + Job())
    private val webViewClient = RewardLabWebViewClient(allowedHost = "10.0.2.2", allowedPort = 8001)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        statusText = TextView(this).apply {
            text = "ADBAP Android Standalone Worker: Ready"
            textSize = 16f
        }
        setContentView(statusText)

        webView = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.cacheMode = WebSettings.LOAD_NO_CACHE
            this.webViewClient = this@AndroidWorkerActivity.webViewClient
        }

        // Kick off minimal workflow execution in foreground
        executeMinimalRewardLabWorkflow()
    }

    private fun executeMinimalRewardLabWorkflow() {
        val executionId = "exec_android_" + UUID.randomUUID().toString().take(8)
        val baseUrl = "http://10.0.2.2:8001"
        val syntheticAccount = "test_01"

        statusText.text = "Executing workflow $executionId on $syntheticAccount..."

        scope.launch {
            val startTime = System.currentTimeMillis()
            var step1Success = false
            var step2Success = false
            var step3Success = false
            val unverifiedMutations = mutableListOf<String>()
            val verifiedMutations = mutableListOf<String>()

            try {
                // Step 1: Navigate to Task Page
                val navResult = webViewClient.navigateAndWait(webView, "$baseUrl/tasks/task_1")
                if (navResult.isSuccess) {
                    step1Success = true
                    Log.i(tag, "Step 1 (navigate) SUCCESS")
                } else {
                    throw navResult.exceptionOrNull() ?: Exception("Navigation failed")
                }

                // Step 2: Click Review Button (Mutating Action)
                unverifiedMutations.add("step_click_review")
                val clickResult = webViewClient.clickElement(webView, "Mark as reviewed")
                if (clickResult.isSuccess) {
                    step2Success = true
                    Log.i(tag, "Step 2 (click mutating) SUCCESS")
                } else {
                    throw clickResult.exceptionOrNull() ?: Exception("Click action failed")
                }

                // Step 3: Causal State Verification
                val verifyResult = webViewClient.verifyElementText(webView, "task-status", "Reviewed")
                if (verifyResult.isSuccess) {
                    step3Success = true
                    verifiedMutations.add("step_click_review")
                    unverifiedMutations.remove("step_click_review")
                    Log.i(tag, "Step 3 (verify_state) SUCCESS -> STATE_VERIFIED")
                } else {
                    throw verifyResult.exceptionOrNull() ?: Exception("Postcondition verification failed")
                }

                val duration = (System.currentTimeMillis() - startTime) / 1000.0
                statusText.text = "Outcome: STATE_VERIFIED in ${duration}s. Standalone execution complete!"

            } catch (ex: Exception) {
                val duration = (System.currentTimeMillis() - startTime) / 1000.0
                Log.e(tag, "Workflow halted: ${ex.message}")

                if (unverifiedMutations.isNotEmpty()) {
                    statusText.text = "Outcome: PAUSED_FOR_REVIEW (SIDE_EFFECT_AMBIGUOUS). Unverified: $unverifiedMutations"
                } else {
                    statusText.text = "Outcome: FAILED (${ex.message})"
                }
            } finally {
                // Clean up session cookies to maintain cross-account isolation
                CookieManager.getInstance().removeAllCookies(null)
                CookieManager.getInstance().flush()
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        webView.destroy()
    }
}
