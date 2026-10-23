package org.adbap.android.worker

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.util.Log
import android.view.View
import android.webkit.CookieManager
import android.webkit.WebSettings
import android.webkit.WebView
import android.widget.Button
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.RadioGroup
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.URI
import java.util.UUID

/**
 * AndroidWorkerActivity: Native Android execution host for ADBAP standalone browser workflows.
 * 
 * Invariants implemented:
 * 1. Fully embedded in view hierarchy with live WebView surface.
 * 2. Strict lifecycle bounds: cancels coroutines and cleans up WebView on destruction.
 * 3. Deny-by-default network policy with selectable verified test endpoints.
 * 4. Two-tier testing: Read-Only (/ui-fixtures) and Causal Verified Mutation (/tasks/task_01).
 * 5. Operator-assisted synthetic login; no hardcoded credentials or token logging.
 * 6. Exact JSON postcondition parsing (replaces raw string containment).
 * 7. Zero blind retry on ambiguous outcomes (OUTCOME_UNCONFIRMED).
 */
class AndroidWorkerActivity : Activity() {

    private val tag = "ADBAP_WorkerActivity"

    // Views
    private lateinit var tvCurrentStep: TextView
    private lateinit var tvOutcome: TextView
    private lateinit var tvLogs: TextView
    private lateinit var progressBar: ProgressBar
    private lateinit var btnStartReadOnly: Button
    private lateinit var btnStartMutation: Button
    private lateinit var btnLogin: Button
    private lateinit var btnStop: Button
    private lateinit var rgEndpoints: RadioGroup
    private lateinit var webView: WebView

    // Execution State
    private var activityJob = Job()
    private var coroutineScope = CoroutineScope(Dispatchers.Main + activityJob)
    private var activeWorkflowJob: Job? = null
    private var hasExecutedMutation = false

    private lateinit var webViewClient: RewardLabWebViewClient
    private var currentBaseUri: URI = URI(RewardLabNetworkConfig.ENDPOINT_LAN)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_worker)

        // Restore state if recreated
        if (savedInstanceState != null) {
            hasExecutedMutation = savedInstanceState.getBoolean("hasExecutedMutation", false)
        }

        bindViews()
        setupNetworkSelection()
        setupWebView()
        setupListeners()

        appendLog("System ready. minSdk=26 (Android 8.0 LG-H872 compatible).")
    }

    override fun onSaveInstanceState(outState: Bundle) {
        super.onSaveInstanceState(outState)
        outState.putBoolean("hasExecutedMutation", hasExecutedMutation)
    }

    private fun bindViews() {
        tvCurrentStep = findViewById(R.id.tvCurrentStep)
        tvOutcome = findViewById(R.id.tvOutcome)
        tvLogs = findViewById(R.id.tvLogs)
        progressBar = findViewById(R.id.progressBar)
        btnStartReadOnly = findViewById(R.id.btnStartReadOnly)
        btnStartMutation = findViewById(R.id.btnStartMutation)
        btnLogin = findViewById(R.id.btnLogin)
        btnStop = findViewById(R.id.btnStop)
        rgEndpoints = findViewById(R.id.rgEndpoints)
        webView = findViewById(R.id.webView)
    }

    private fun setupNetworkSelection() {
        currentBaseUri = URI(RewardLabNetworkConfig.ENDPOINT_LAN)
        webViewClient = RewardLabWebViewClient(currentBaseUri)

        rgEndpoints.setOnCheckedChangeListener { _, checkedId ->
            val endpointStr = when (checkedId) {
                R.id.rbEndpointLan -> RewardLabNetworkConfig.ENDPOINT_LAN
                R.id.rbEndpointWifi -> RewardLabNetworkConfig.ENDPOINT_WIFI
                R.id.rbEndpointReverse -> RewardLabNetworkConfig.ENDPOINT_REVERSE
                R.id.rbEndpointEmulator -> RewardLabNetworkConfig.ENDPOINT_EMULATOR
                else -> RewardLabNetworkConfig.ENDPOINT_LAN
            }
            val validation = RewardLabNetworkConfig.validateTargetUrl(endpointStr)
            if (validation.isSuccess) {
                currentBaseUri = validation.getOrThrow()
                webViewClient.updateAllowedBaseUri(currentBaseUri)
                appendLog("Target endpoint updated: $currentBaseUri")
            } else {
                appendLog("Error updating endpoint: ${validation.exceptionOrNull()?.message}")
            }
        }
    }

    private fun setupWebView() {
        webView.settings.apply {
            javaScriptEnabled = true
            domStorageEnabled = true
            cacheMode = WebSettings.LOAD_NO_CACHE
            setSupportMultipleWindows(false)
            allowFileAccess = false
            allowContentAccess = false
        }
        webView.webViewClient = this.webViewClient
    }

    private fun setupListeners() {
        btnStartReadOnly.setOnClickListener {
            runReadOnlyWorkflow()
        }

        btnStartMutation.setOnClickListener {
            runVerifiedMutationWorkflow()
        }

        btnLogin.setOnClickListener {
            openLoginPage()
        }

        btnStop.setOnClickListener {
            stopActiveWorkflow("Cancelled by operator")
        }
    }

    private fun openLoginPage() {
        val loginUrl = "$currentBaseUri/login"
        appendLog("Navigating to login page: $loginUrl")
        tvCurrentStep.text = "Current Step: Manual Synthetic Login"
        tvOutcome.text = "Outcome: AWAITING_OPERATOR_LOGIN"
        tvOutcome.setTextColor(Color.parseColor("#0284C7"))

        webView.loadUrl(loginUrl)
    }

    /**
     * Test 1 — Minimal Read-Only WebView Automation
     * Targets /ui-fixtures, waits for delayed element, extracts and verifies DOM text.
     */
    private fun runReadOnlyWorkflow() {
        val workflowId = "wf_readonly_" + UUID.randomUUID().toString().take(6)
        val targetUrl = "$currentBaseUri/ui-fixtures"

        setExecutionInProgress(true, "Running Test 1 (Read-Only Fixtures)...")
        appendLog("[$workflowId] Starting Read-Only Test on $targetUrl")

        activeWorkflowJob = coroutineScope.launch {
            val startTime = System.currentTimeMillis()
            try {
                // Step 1: Navigate to /ui-fixtures
                updateStep("1/3: Navigating to /ui-fixtures")
                appendLog("Navigating to $targetUrl...")
                val navResult = webViewClient.navigateAndWait(webView, targetUrl, timeoutMs = 8000L)
                if (navResult.isFailure) {
                    throw navResult.exceptionOrNull() ?: Exception("Navigation failed")
                }
                appendLog("Page loaded successfully.")

                // Step 2: Await dynamically rendered element (delayed-btn renders after 350ms)
                updateStep("2/3: Awaiting dynamic element [data-testid='delayed-btn']")
                appendLog("Waiting for [data-testid='delayed-btn']...")
                val waitResult = webViewClient.waitForElement(webView, "delayed-btn", timeoutMs = 4000L)
                if (waitResult.isFailure) {
                    throw waitResult.exceptionOrNull() ?: Exception("Element wait timed out")
                }
                appendLog("Element attached to DOM.")

                // Step 3: Extract and verify text
                updateStep("3/3: Extracting DOM text content")
                val textResult = webViewClient.extractElementText(webView, "delayed-btn")
                if (textResult.isFailure) {
                    throw textResult.exceptionOrNull() ?: Exception("DOM extraction failed")
                }

                val extracted = textResult.getOrThrow()
                appendLog("Extracted text: '$extracted'")

                val expected = "Delayed Button Ready"
                if (extracted == expected) {
                    val duration = (System.currentTimeMillis() - startTime) / 1000.0
                    tvOutcome.text = "Outcome: READ_ONLY_VERIFIED (${duration}s)"
                    tvOutcome.setTextColor(Color.parseColor("#16A34A"))
                    appendLog("[$workflowId] PASS: Exact text match '$expected'. Zero side-effects.")
                } else {
                    tvOutcome.text = "Outcome: STATE_MISMATCH"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("[$workflowId] FAIL: Expected '$expected' but found '$extracted'")
                }

            } catch (ex: Exception) {
                handleExecutionFailure(workflowId, ex)
            } finally {
                setExecutionInProgress(false)
            }
        }
    }

    /**
     * Test 2 — Verified Causal Mutation
     * Pre-checks auth, verifies pending state, clicks review, verifies postcondition.
     */
    private fun runVerifiedMutationWorkflow() {
        val workflowId = "wf_mutation_" + UUID.randomUUID().toString().take(6)
        val targetUrl = "$currentBaseUri/tasks/task_01"

        setExecutionInProgress(true, "Running Test 2 (Verified Mutation task_01)...")
        appendLog("[$workflowId] Starting Mutation Test on $targetUrl")

        activeWorkflowJob = coroutineScope.launch {
            val startTime = System.currentTimeMillis()
            val unverifiedMutations = mutableListOf<String>()
            val verifiedMutations = mutableListOf<String>()

            try {
                // Step 1: Navigate to task_01
                updateStep("1/4: Navigating to /tasks/task_01")
                appendLog("Navigating to $targetUrl...")
                val navResult = webViewClient.navigateAndWait(webView, targetUrl, timeoutMs = 8000L)
                if (navResult.isFailure) {
                    throw navResult.exceptionOrNull() ?: Exception("Navigation failed")
                }

                // Check if redirected to login (synthetic authentication required)
                val currentUrl = webView.url ?: ""
                if (currentUrl.contains("/login")) {
                    tvOutcome.text = "Outcome: AUTH_REQUIRED"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("Redirected to login. Please log in first via 'Open Login Page'.")
                    return@launch
                }

                // Step 2: Verify pre-condition (Task must be Pending)
                updateStep("2/4: Verifying pre-condition (Task Pending)")
                val preState = webViewClient.verifyTaskDetailState(webView).getOrNull()
                if (preState == null || !preState.found) {
                    tvOutcome.text = "Outcome: ELEMENT_NOT_FOUND"
                    tvOutcome.setTextColor(Color.parseColor("#DC2626"))
                    appendLog("Task detail elements not found. Precondition failed.")
                    return@launch
                }

                if (preState.isReviewed) {
                    appendLog("Task task_01 is already 'Reviewed'. Idempotent / already completed state.")
                } else if (!preState.isPending) {
                    appendLog("Unexpected initial status: ${preState.statusText}")
                } else {
                    appendLog("Precondition verified: task_01 status is 'Pending'.")
                }

                // Step 3: Mutating Action - Click 'mark-reviewed-btn'
                updateStep("3/4: Dispatched click on [data-testid='mark-reviewed-btn']")
                unverifiedMutations.add("mark-reviewed-btn")
                hasExecutedMutation = true

                val clickResult = webViewClient.clickElementByTestId(webView, "mark-reviewed-btn")
                if (clickResult.isFailure) {
                    throw clickResult.exceptionOrNull() ?: Exception("Click action dispatch failed")
                }
                appendLog("Click dispatched successfully. Awaiting causal server outcome...")

                // Brief propagation delay for DOM/network update
                delay(800L)

                // Step 4: Causal State Verification
                updateStep("4/4: Evaluating causal postcondition state")
                val postState = webViewClient.verifyTaskDetailState(webView).getOrNull()

                if (postState != null && postState.found && postState.isReviewed) {
                    // Causal postcondition explicitly satisfied
                    verifiedMutations.add("mark-reviewed-btn")
                    unverifiedMutations.remove("mark-reviewed-btn")

                    val duration = (System.currentTimeMillis() - startTime) / 1000.0
                    tvOutcome.text = "Outcome: STATE_VERIFIED (${duration}s)"
                    tvOutcome.setTextColor(Color.parseColor("#16A34A"))
                    appendLog("[$workflowId] PASS: Status 'Reviewed' verified with postcondition evidence.")
                } else {
                    // Ambiguous outcome: mutation occurred but state is unconfirmed. DO NOT BLIND RETRY.
                    val duration = (System.currentTimeMillis() - startTime) / 1000.0
                    tvOutcome.text = "Outcome: OUTCOME_UNCONFIRMED (${duration}s)"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("[$workflowId] HALTED: Postcondition not confirmed. Unverified: $unverifiedMutations. Halting without retry.")
                }

            } catch (ex: Exception) {
                if (unverifiedMutations.isNotEmpty()) {
                    tvOutcome.text = "Outcome: OUTCOME_UNCONFIRMED"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("[$workflowId] AMBIGUOUS: Mutation attempted but failed verification: ${ex.message}")
                } else {
                    handleExecutionFailure(workflowId, ex)
                }
            } finally {
                setExecutionInProgress(false)
            }
        }
    }

    private fun stopActiveWorkflow(reason: String) {
        activeWorkflowJob?.cancel()
        activeWorkflowJob = null
        webView.stopLoading()
        setExecutionInProgress(false)
        tvOutcome.text = "Outcome: STOPPED_BY_OPERATOR"
        tvOutcome.setTextColor(Color.parseColor("#DC2626"))
        appendLog("Workflow halted: $reason")
    }

    private fun handleExecutionFailure(workflowId: String, ex: Exception) {
        val msg = ex.message ?: "Unknown error"
        Log.e(tag, "[$workflowId] Execution failed: $msg", ex)
        tvOutcome.text = "Outcome: EXECUTION_FAILED"
        tvOutcome.setTextColor(Color.parseColor("#DC2626"))
        appendLog("[$workflowId] ERROR: $msg")
    }

    private fun setExecutionInProgress(inProgress: Boolean, stepDesc: String = "") {
        progressBar.visibility = if (inProgress) View.VISIBLE else View.GONE
        btnStartReadOnly.isEnabled = !inProgress
        btnStartMutation.isEnabled = !inProgress
        btnLogin.isEnabled = !inProgress
        btnStop.isEnabled = inProgress

        for (i in 0 until rgEndpoints.childCount) {
            rgEndpoints.getChildAt(i).isEnabled = !inProgress
        }

        if (inProgress) {
            tvCurrentStep.text = stepDesc
        }
    }

    private fun updateStep(stepText: String) {
        tvCurrentStep.text = "Current Step: $stepText"
    }

    private fun appendLog(line: String) {
        val current = tvLogs.text.toString()
        val lines = current.split("\n").filter { it.isNotEmpty() }
        val trimmed = if (lines.size > 25) lines.takeLast(20).joinToString("\n") + "\n" else current
        tvLogs.text = "$trimmed> $line\n"
    }

    override fun onDestroy() {
        super.onDestroy()
        appendLog("Destroying Activity: Cleaning up coroutines and WebView...")
        activeWorkflowJob?.cancel()
        coroutineScope.cancel()

        try {
            webView.stopLoading()
            webView.clearHistory()
            webView.loadUrl("about:blank")
            webView.destroy()
        } catch (e: Exception) {
            Log.e(tag, "Error destroying WebView: ${e.message}")
        }
    }
}
