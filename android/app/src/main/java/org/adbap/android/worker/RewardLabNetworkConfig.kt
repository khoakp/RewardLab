package org.adbap.android.worker

import java.net.URI
import java.util.regex.Pattern

/**
 * Enforces deny-by-default network policy for RewardLab automation.
 * Strictly checks scheme, host, port, and path traversal before any WebView load.
 */
object RewardLabNetworkConfig {

    const val ENDPOINT_LAN = "http://192.168.1.122:8001"
    const val ENDPOINT_WIFI = "http://172.16.1.2:8001"
    const val ENDPOINT_REVERSE = "http://127.0.0.1:8001"
    const val ENDPOINT_EMULATOR = "http://10.0.2.2:8001"

    private val ALLOWED_PORTS = setOf(8001, 80, 443)
    private val ALLOWED_SCHEMES = setOf("http", "https")

    // Approved private IPv4 patterns & loopbacks for test execution
    private val PRIVATE_IP_PATTERN = Pattern.compile(
        "^(" +
        "127\\.0\\.0\\.1|" +
        "localhost|" +
        "10\\.\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}|" +
        "172\\.(1[6-9]|2[0-9]|3[0-1])\\.\\d{1,3}\\.\\d{1,3}|" +
        "192\\.168\\.\\d{1,3}\\.\\d{1,3}" +
        ")$"
    )

    /**
     * Validates and returns a normalized URI if it satisfies the strict default-deny policy.
     */
    fun validateTargetUrl(rawUrl: String): Result<URI> {
        val trimmed = rawUrl.trim()
        if (trimmed.isEmpty()) {
            return Result.failure(IllegalArgumentException("Target URL cannot be empty"))
        }

        // Reject scheme-relative URLs (e.g. "//evil.com")
        if (trimmed.startsWith("//")) {
            return Result.failure(SecurityException("Scheme-relative URLs are prohibited"))
        }

        val uri = try {
            URI(trimmed)
        } catch (e: Exception) {
            return Result.failure(IllegalArgumentException("Malformed target URL: ${e.message}"))
        }

        val scheme = uri.scheme?.lowercase()
        if (scheme == null || scheme !in ALLOWED_SCHEMES) {
            return Result.failure(SecurityException("Scheme '$scheme' is not permitted. Only HTTP/HTTPS allowed."))
        }

        val host = uri.host?.lowercase()
        if (host.isNullOrEmpty()) {
            return Result.failure(SecurityException("Host is missing in target URL"))
        }

        // Deny embedded credentials (userinfo, e.g. "http://user:pass@host")
        if (!uri.userInfo.isNullOrEmpty()) {
            return Result.failure(SecurityException("Embedded credentials in URL are prohibited"))
        }

        // Port resolution: default 80 for http, 443 for https
        val port = if (uri.port != -1) uri.port else if (scheme == "https") 443 else 80
        if (port !in ALLOWED_PORTS) {
            return Result.failure(SecurityException("Port $port is not permitted. Allowed ports: $ALLOWED_PORTS"))
        }

        // Host validation: must be an approved private IPv4, localhost, or emulator loopback
        if (!PRIVATE_IP_PATTERN.matcher(host).matches()) {
            return Result.failure(SecurityException("Host '$host' is not an authorized RewardLab test target (deny-by-default)"))
        }

        // Path validation: prevent directory traversal
        val path = uri.path ?: "/"
        if (path.contains("..")) {
            return Result.failure(SecurityException("Path traversal ('..') is strictly prohibited"))
        }

        return Result.success(uri)
    }

    /**
     * Checks if a requested destination URL is within the approved target origin.
     */
    fun isOriginAllowed(destinationUri: URI, allowedBaseUri: URI): Boolean {
        val destHost = destinationUri.host?.lowercase() ?: return false
        val baseHost = allowedBaseUri.host?.lowercase() ?: return false

        val destPort = if (destinationUri.port != -1) destinationUri.port else if (destinationUri.scheme == "https") 443 else 80
        val basePort = if (allowedBaseUri.port != -1) allowedBaseUri.port else if (allowedBaseUri.scheme == "https") 443 else 80

        val destScheme = destinationUri.scheme?.lowercase() ?: return false
        val baseScheme = allowedBaseUri.scheme?.lowercase() ?: return false

        return destHost == baseHost && destPort == basePort && destScheme == baseScheme
    }
}
