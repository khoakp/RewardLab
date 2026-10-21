package org.adbap.android.worker

import android.graphics.Bitmap
import android.os.Handler
import android.os.Looper
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient
import java.net.URI
import kotlin.coroutines.resume
import kotlin.coroutines.suspendCancellableCoroutine

/**
 * Custom WebViewClient that enforces provider allowlist and facilitates
 * asynchronous DOM navigation and evaluation without desktop Playwright.
 */
class RewardLabWebViewClient(
    private val allowedHost: String = "10.0.2.2", // Android Emulator loopback or local LAN IP
    private val allowedPort: Int = 8001
) : WebViewClient() {

    private var pageLoadedCallback: ((Boolean, String?) -> Unit)? = null
    private val mainHandler = Handler(Looper.getMainLooper())

    override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
        val uri = request?.url ?: return true
        val host = uri.host
        val port = if (uri.port != -1) uri.port else 80

        // Strict Domain and Port Allowlist check (Zero foreign navigation)
        if (host != allowedHost || port != allowedPort) {
            return true // Block navigation outside allowlist
        }
        return false // Allow loading within allowlist
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
            pageLoadedCallback?.invoke(false, description)
            pageLoadedCallback = null
        }
    }

    /**
     * Coroutine-based navigation awaiting page load finish.
     */
    suspend fun navigateAndWait(webView: WebView, targetUrl: String, timeoutMs: Long = 10000L): Result<Unit> {
        return suspendCancellableCoroutine { continuation ->
            val timeoutRunnable = Runnable {
                pageLoadedCallback = null
                if (continuation.isActive) {
                    continuation.resume(Result.failure(Exception("Navigation timeout after ${timeoutMs}ms")))
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

            mainHandler.postDelayed(timeoutRunnable, timeoutMs)
            webView.loadUrl(targetUrl)
        }
    }

    /**
     * Executes asynchronous DOM click by evaluating selector and dispatching click event.
     */
    suspend fun clickElement(webView: WebView, testId: String): Result<Boolean> {
        val script = """
            (function() {
                var el = document.querySelector('[data-testid="' + '$testId' + '"]') ||
                         Array.from(document.querySelectorAll('button')).find(b => b.innerText.trim() === '$testId');
                if (!el) return JSON.stringify({success: false, error: 'Element not found'});
                el.click();
                return JSON.stringify({success: true});
            })();
        """.trimIndent()

        return suspendCancellableCoroutine { continuation ->
            webView.evaluateJavascript(script) { resultJson ->
                val clean = resultJson?.replace("^\"|\"$".toRegex(), "")?.replace("\\\"", "\"")
                if (clean != null && clean.contains("\"success\":true")) {
                    continuation.resume(Result.success(true))
                } else {
                    continuation.resume(Result.failure(Exception("Click failed: $clean")))
                }
            }
        }
    }

    /**
     * Verifies postcondition by checking DOM state.
     */
    suspend fun verifyElementText(webView: WebView, testId: String, expectedText: String): Result<Boolean> {
        val script = """
            (function() {
                var el = document.querySelector('[data-testid="' + '$testId' + '"]');
                if (!el) return JSON.stringify({found: false});
                return JSON.stringify({found: true, text: el.innerText.trim()});
            })();
        """.trimIndent()

        return suspendCancellableCoroutine { continuation ->
            webView.evaluateJavascript(script) { resultJson ->
                if (resultJson != null && resultJson.contains(expectedText)) {
                    continuation.resume(Result.success(true))
                } else {
                    continuation.resume(Result.failure(Exception("State verification failed for $testId")))
                }
            }
        }
    }
}
