package org.adbap.android.worker

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.util.Log
import android.view.View
import android.webkit.WebSettings
import android.webkit.WebView
import android.widget.Button
import android.widget.ProgressBar
import android.widget.RadioGroup
import android.widget.TextView
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import java.net.URI
import java.util.UUID

class AndroidWorkerActivity : Activity() {

    private val tag = "ADBAP_WorkerActivity"

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

    private var activityJob = Job()
    private var coroutineScope = CoroutineScope(Dispatchers.Main + activityJob)
    private var activeWorkflowJob: Job? = null
    private var hasExecutedMutation = false

    private lateinit var webViewClient: RewardLabWebViewClient
    private var currentBaseUri: URI = URI(RewardLabNetworkConfig.ENDPOINT_LAN)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_worker)

        if (savedInstanceState != null) {
            hasExecutedMutation = savedInstanceState.getBoolean("hasExecutedMutation", false)
        }

        bindViews()
        setupNetworkSelection()
        setupWebView()
        setupListeners()
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
                appendLog("Target endpoint: $currentBaseUri")
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
        btnStartReadOnly.setOnClickListener { runReadOnlyWorkflow() }
        btnStartMutation.setOnClickListener { runVerifiedMutationWorkflow() }
        btnLogin.setOnClickListener { openLoginPage() }
        btnStop.setOnClickListener { stopActiveWorkflow("Cancelled by operator") }
    }

    private fun openLoginPage() {
        val loginUrl = "$currentBaseUri/login"
        tvCurrentStep.text = "Current Step: Manual Synthetic Login"
        tvOutcome.text = "Outcome: AWAITING_OPERATOR_LOGIN"
        tvOutcome.setTextColor(Color.parseColor("#0284C7"))
        webView.loadUrl(loginUrl)
    }

    private fun runReadOnlyWorkflow() {
        val workflowId = "wf_readonly_" + UUID.randomUUID().toString().take(6)
        val targetUrl = "$currentBaseUri/ui-fixtures"

        setExecutionInProgress(true, "Running Test 1 (Read-Only Fixtures)...")
        appendLog("[$workflowId] Starting Read-Only Test on $targetUrl")

        activeWorkflowJob = coroutineScope.launch {
            val startTime = System.currentTimeMillis()
            try {
                updateStep("1/3: Navigating to /ui-fixtures")
                val navResult = webViewClient.navigateAndWait(webView, targetUrl, timeoutMs = 8000L)
                if (navResult.isFailure) throw navResult.exceptionOrNull() ?: Exception("Navigation failed")

                updateStep("2/3: Awaiting [data-testid='delayed-btn']")
                val waitResult = webViewClient.waitForElement(webView, "delayed-btn", timeoutMs = 4000L)
                if (waitResult.isFailure) throw waitResult.exceptionOrNull() ?: Exception("Wait timed out")

                updateStep("3/3: Extracting DOM text")
                val textResult = webViewClient.extractElementText(webView, "delayed-btn")
                if (textResult.isFailure) throw textResult.exceptionOrNull() ?: Exception("Extraction failed")

                val extracted = textResult.getOrThrow()
                val expected = "Delayed Button Ready"
                if (extracted == expected) {
                    val duration = (System.currentTimeMillis() - startTime) / 1000.0
                    tvOutcome.text = "Outcome: READ_ONLY_VERIFIED (${duration}s)"
                    tvOutcome.setTextColor(Color.parseColor("#16A34A"))
                    appendLog("[$workflowId] PASS: Exact text match '$expected'.")
                } else {
                    tvOutcome.text = "Outcome: STATE_MISMATCH"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("[$workflowId] FAIL: Found '$extracted'")
                }
            } catch (ex: Exception) {
                handleExecutionFailure(workflowId, ex)
            } finally {
                setExecutionInProgress(false)
            }
        }
    }

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
                updateStep("1/4: Navigating to /tasks/task_01")
                val navResult = webViewClient.navigateAndWait(webView, targetUrl, timeoutMs = 8000L)
                if (navResult.isFailure) throw navResult.exceptionOrNull() ?: Exception("Navigation failed")

                val currentUrl = webView.url ?: ""
                if (currentUrl.contains("/login")) {
                    tvOutcome.text = "Outcome: AUTH_REQUIRED"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("Redirected to login. Please log in first via 'Open Login Page'.")
                    return@launch
                }

                updateStep("2/4: Verifying pre-condition (Pending)")
                val preState = webViewClient.verifyTaskDetailState(webView).getOrNull()
                if (preState == null || !preState.found) {
                    tvOutcome.text = "Outcome: ELEMENT_NOT_FOUND"
                    tvOutcome.setTextColor(Color.parseColor("#DC2626"))
                    return@launch
                }

                updateStep("3/4: Dispatched click on mark-reviewed-btn")
                unverifiedMutations.add("mark-reviewed-btn")
                hasExecutedMutation = true

                val clickResult = webViewClient.clickElementByTestId(webView, "mark-reviewed-btn")
                if (clickResult.isFailure) throw clickResult.exceptionOrNull() ?: Exception("Click failed")

                delay(800L)

                updateStep("4/4: Evaluating causal postcondition")
                val postState = webViewClient.verifyTaskDetailState(webView).getOrNull()

                if (postState != null && postState.found && postState.isReviewed) {
                    verifiedMutations.add("mark-reviewed-btn")
                    unverifiedMutations.remove("mark-reviewed-btn")

                    val duration = (System.currentTimeMillis() - startTime) / 1000.0
                    tvOutcome.text = "Outcome: STATE_VERIFIED (${duration}s)"
                    tvOutcome.setTextColor(Color.parseColor("#16A34A"))
                    appendLog("[$workflowId] PASS: Status 'Reviewed' verified.")
                } else {
                    val duration = (System.currentTimeMillis() - startTime) / 1000.0
                    tvOutcome.text = "Outcome: OUTCOME_UNCONFIRMED"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
                    appendLog("[$workflowId] HALTED: Postcondition not confirmed. No blind retry.")
                }
            } catch (ex: Exception) {
                if (unverifiedMutations.isNotEmpty()) {
                    tvOutcome.text = "Outcome: OUTCOME_UNCONFIRMED"
                    tvOutcome.setTextColor(Color.parseColor("#D97706"))
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
        appendLog("Workflow stopped: $reason")
    }

    private fun handleExecutionFailure(workflowId: String, ex: Exception) {
        val msg = ex.message ?: "Unknown error"
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
        if (inProgress) tvCurrentStep.text = stepDesc
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
