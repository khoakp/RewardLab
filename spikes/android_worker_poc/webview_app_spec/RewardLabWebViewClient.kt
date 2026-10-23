package org.adbap.android.worker

import android.net.http.SslError
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient
import kotlinx.coroutines.suspendCancellableCoroutine
import org.json.JSONObject
import java.net.URI
import kotlin.coroutines.resume

/**
 * Hardened WebViewClient enforcing origin allowlist, SSL integrity,
 * coroutine-based navigation, and structured DOM verification.
 */
class RewardLabWebViewClient(
    private var allowedBaseUri: URI
) : WebViewClient() {

    private val tag = "ADBAP_WebViewClient"
    private var pageLoadedCallback: ((Boolean, String?) -> Unit)? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    fun updateAllowedBaseUri(newBaseUri: URI) {
        this.allowedBaseUri = newBaseUri
    }

    override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
        val url = request?.url ?: return true
        val uri = try {
            URI(url.toString())
        } catch (e: Exception) {
            Log.e(tag, "Blocked malformed navigation attempt: $url")
            return true
        }

        if (!RewardLabNetworkConfig.isOriginAllowed(uri, allowedBaseUri)) {
            Log.w(tag, "SECURITY DENIAL: Blocked navigation outside allowlist origin. Target: $uri, Allowed: $allowedBaseUri")
            return true // Deny navigation outside origin
        }
        return false // Allow navigation within authorized origin
    }

    override fun onPageFinished(view: WebView?, url: String?) {
        super.onPageFinished(view, url)
        pageLoadedCallback?.invoke(true, null)
        pageLoadedCallback = null
    }

    override fun onReceivedError(view: WebView?, request: WebResourceRequest?, error: WebResourceError?) {
        super.onReceivedError(view, request, error)
        if (request?.isForMainFrame == true) {
            val description = error?.description?.toString() ?: "Unknown error"
            Log.e(tag, "Main frame navigation failed: $description")
            pageLoadedCallback?.invoke(false, description)
            pageLoadedCallback = null
        }
    }

    override fun onReceivedHttpError(view: WebView?, request: WebResourceRequest?, errorResponse: WebResourceResponse?) {
        super.onReceivedHttpError(view, request, errorResponse)
        if (request?.isForMainFrame == true) {
            val statusCode = errorResponse?.statusCode ?: -1
            Log.w(tag, "HTTP error on main frame: $statusCode")
            if (statusCode in 400..599) {
                pageLoadedCallback?.invoke(false, "HTTP $statusCode")
                pageLoadedCallback = null
            }
        }
    }

    override fun onReceivedSslError(view: WebView?, handler: SslErrorHandler?, error: SslError?) {
        // Strict security rule: NEVER ignore SSL errors
        Log.e(tag, "SECURITY: SSL Certificate validation error (${error?.primaryError}). Cancelling navigation.")
        handler?.cancel()
        pageLoadedCallback?.invoke(false, "SSL Error: ${error?.primaryError}")
        pageLoadedCallback = null
    }

    /**
     * Coroutine-based page navigation with strict timeout and URL pre-flight validation.
     */
    suspend fun navigateAndWait(webView: WebView, targetUrl: String, timeoutMs: Long = 10000L): Result<Unit> {
        val validation = RewardLabNetworkConfig.validateTargetUrl(targetUrl)
        if (validation.isFailure) {
            return Result.failure(validation.exceptionOrNull() ?: SecurityException("URL validation failed"))
        }

        return suspendCancellableCoroutine<Result<Unit>> { continuation ->
            val timeoutRunnable = Runnable {
                pageLoadedCallback = null
                if (continuation.isActive) {
                    continuation.resume(Result.failure(Exception("Navigation timeout after ${timeoutMs}ms to $targetUrl")))
                }
            }

            pageLoadedCallback = { success, errorMsg ->
                mainHandler.removeCallbacks(timeoutRunnable)
                if (continuation.isActive) {
                    if (success) {
                        continuation.resume(Result.success(Unit))
                    } else {
                        continuation.resume(Result.failure(Exception("Page load failed: $errorMsg")))
                    }
                }
            }

            continuation.invokeOnCancellation {
                mainHandler.removeCallbacks(timeoutRunnable)
                pageLoadedCallback = null
            }

            mainHandler.postDelayed(timeoutRunnable, timeoutMs)
            webView.loadUrl(targetUrl)
        }
    }

    /**
     * Polls asynchronously until element matching data-testid is attached to the DOM.
     */
    suspend fun waitForElement(webView: WebView, testId: String, timeoutMs: Long = 5000L): Result<Boolean> {
        val startTime = System.currentTimeMillis()
        val escapedId = escapeForJs(testId)

        while (System.currentTimeMillis() - startTime < timeoutMs) {
            val script = """
                (function() {
                    var el = document.querySelector('[data-testid="$escapedId"]');
                    return JSON.stringify({ found: !!el });
                })();
            """.trimIndent()

            val found = suspendCancellableCoroutine<Boolean> { continuation ->
                webView.evaluateJavascript(script) { resultJson ->
                    val clean = cleanJsonResult(resultJson)
                    val parsed = try {
                        JSONObject(clean).optBoolean("found", false)
                    } catch (e: Exception) {
                        false
                    }
                    if (continuation.isActive) {
                        continuation.resume(parsed)
                    }
                }
            }

            if (found) {
                return Result.success(true)
            }
            kotlinx.coroutines.delay(100L)
        }

        return Result.failure(Exception("Element [data-testid='$testId'] timed out after ${timeoutMs}ms"))
    }

    /**
     * Extracts text content of target element by data-testid with structured JSON parsing.
     */
    suspend fun extractElementText(webView: WebView, testId: String): Result<String> {
        val escapedId = escapeForJs(testId)
        val script = """
            (function() {
                var el = document.querySelector('[data-testid="$escapedId"]');
                if (!el) return JSON.stringify({ found: false });
                return JSON.stringify({ found: true, text: (el.innerText || el.textContent || '').trim() });
            })();
        """.trimIndent()

        return suspendCancellableCoroutine<Result<String>> { continuation ->
            webView.evaluateJavascript(script) { resultJson ->
                val clean = cleanJsonResult(resultJson)
                try {
                    val obj = JSONObject(clean)
                    if (obj.optBoolean("found", false)) {
                        val text = obj.optString("text", "")
                        if (continuation.isActive) {
                            continuation.resume(Result.success(text))
                        }
                    } else {
                        if (continuation.isActive) {
                            continuation.resume(Result.failure(Exception("Element [data-testid='$testId'] not found")))
                        }
                    }
                } catch (e: Exception) {
                    if (continuation.isActive) {
                        continuation.resume(Result.failure(Exception("Failed to parse DOM result: $clean (${e.message})")))
                    }
                }
            }
        }
    }

    /**
     * Safely clicks target button identified by data-testid.
     */
    suspend fun clickElementByTestId(webView: WebView, testId: String): Result<Boolean> {
        val escapedId = escapeForJs(testId)
        val script = """
            (function() {
                var el = document.querySelector('[data-testid="$escapedId"]');
                if (!el) return JSON.stringify({ success: false, error: 'ELEMENT_NOT_FOUND' });
                if (el.disabled) return JSON.stringify({ success: false, error: 'ELEMENT_DISABLED' });
                el.click();
                return JSON.stringify({ success: true });
            })();
        """.trimIndent()

        return suspendCancellableCoroutine<Result<Boolean>> { continuation ->
            webView.evaluateJavascript(script) { resultJson ->
                val clean = cleanJsonResult(resultJson)
                try {
                    val obj = JSONObject(clean)
                    if (obj.optBoolean("success", false)) {
                        if (continuation.isActive) {
                            continuation.resume(Result.success(true))
                        }
                    } else {
                        val err = obj.optString("error", "CLICK_FAILED")
                        if (continuation.isActive) {
                            continuation.resume(Result.failure(Exception("Click failed: $err")))
                        }
                    }
                } catch (e: Exception) {
                    if (continuation.isActive) {
                        continuation.resume(Result.failure(Exception("Click evaluation error: $clean (${e.message})")))
                    }
                }
            }
        }
    }

    /**
     * Performs verified outcome inspection of RewardLab task_detail page.
     * Evaluates status badge, success alert, and button state in a single atomic snapshot.
     */
    suspend fun verifyTaskDetailState(webView: WebView): Result<DomVerificationResult> {
        val script = """
            (function() {
                var statusEl = document.querySelector('[data-testid="task-status"]');
                var alertEl = document.querySelector('[data-testid="review-success-alert"]');
                var btnEl = document.querySelector('[data-testid="mark-reviewed-btn"]');
                
                if (!statusEl) {
                    return JSON.stringify({
                        found: false,
                        errorMessage: 'TASK_STATUS_ELEMENT_MISSING'
                    });
                }

                var statusText = (statusEl.innerText || statusEl.textContent || '').trim();
                var hasSuccessAlert = !!alertEl;
                var buttonDisabled = !!(btnEl && btnEl.disabled);

                return JSON.stringify({
                    found: true,
                    statusText: statusText,
                    hasSuccessAlert: hasSuccessAlert,
                    buttonDisabled: buttonDisabled,
                    isPending: statusText === 'Pending',
                    isReviewed: statusText === 'Reviewed'
                });
            })();
        """.trimIndent()

        return suspendCancellableCoroutine<Result<DomVerificationResult>> { continuation ->
            webView.evaluateJavascript(script) { resultJson ->
                val clean = cleanJsonResult(resultJson)
                try {
                    val obj = JSONObject(clean)
                    val found = obj.optBoolean("found", false)
                    if (!found) {
                        if (continuation.isActive) {
                            continuation.resume(Result.success(DomVerificationResult(
                                found = false,
                                errorMessage = obj.optString("errorMessage", "ELEMENT_NOT_FOUND")
                            )))
                        }
                    } else {
                        if (continuation.isActive) {
                            continuation.resume(Result.success(DomVerificationResult(
                                found = true,
                                statusText = obj.optString("statusText"),
                                hasSuccessAlert = obj.optBoolean("hasSuccessAlert", false),
                                buttonDisabled = obj.optBoolean("buttonDisabled", false),
                                isPending = obj.optBoolean("isPending", false),
                                isReviewed = obj.optBoolean("isReviewed", false)
                            )))
                        }
                    }
                } catch (e: Exception) {
                    if (continuation.isActive) {
                        continuation.resume(Result.failure(Exception("Failed to parse task state verification: $clean (${e.message})")))
                    }
                }
            }
        }
    }

    private fun escapeForJs(input: String): String {
        return input.replace("\\", "\\\\").replace("\"", "\\\"").replace("'", "\\'")
    }

    private fun cleanJsonResult(raw: String?): String {
        if (raw == null || raw == "null") return "{}"
        var s = raw.trim()
        if (s.startsWith("\"") && s.endsWith("\"") && s.length >= 2) {
            s = s.substring(1, s.length - 1)
        }
        return s.replace("\\\"", "\"").replace("\\\\", "\\")
    }
}
