package org.adbap.android.worker

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.URI

class AndroidWorkerUnitTest {

    @Test
    fun testNetworkConfigAllowedEndpoints() {
        val endpoints = listOf(
            RewardLabNetworkConfig.ENDPOINT_LAN,
            RewardLabNetworkConfig.ENDPOINT_WIFI,
            RewardLabNetworkConfig.ENDPOINT_REVERSE,
            RewardLabNetworkConfig.ENDPOINT_EMULATOR
        )

        for (endpoint in endpoints) {
            val res = RewardLabNetworkConfig.validateTargetUrl(endpoint)
            assertTrue("Expected $endpoint to be allowed", res.isSuccess)
        }
    }

    @Test
    fun testNetworkConfigDeniesPublicInternet() {
        val publicUrls = listOf(
            "https://google.com",
            "http://8.8.8.8:8001",
            "http://microsoft.com/rewards",
            "https://evil-attacker.org/tasks"
        )

        for (url in publicUrls) {
            val res = RewardLabNetworkConfig.validateTargetUrl(url)
            assertTrue("Expected $url to be denied", res.isFailure)
        }
    }

    @Test
    fun testNetworkConfigDeniesSchemeRelative() {
        val res = RewardLabNetworkConfig.validateTargetUrl("//192.168.1.122:8001/tasks")
        assertTrue("Expected scheme-relative URL to be denied", res.isFailure)
    }

    @Test
    fun testNetworkConfigDeniesPathTraversal() {
        val res = RewardLabNetworkConfig.validateTargetUrl("http://192.168.1.122:8001/../internal/secrets")
        assertTrue("Expected path traversal URL to be denied", res.isFailure)
    }

    @Test
    fun testNetworkConfigDeniesEmbeddedUserInfo() {
        val res = RewardLabNetworkConfig.validateTargetUrl("http://admin:secret@192.168.1.122:8001/tasks")
        assertTrue("Expected userinfo URL to be denied", res.isFailure)
    }

    @Test
    fun testNetworkConfigOriginAllowedMatching() {
        val base = URI("http://192.168.1.122:8001")
        val matching = URI("http://192.168.1.122:8001/tasks/task_01")
        val wrongPort = URI("http://192.168.1.122:9000/tasks")
        val wrongHost = URI("http://192.168.1.99:8001/tasks")
        val wrongScheme = URI("https://192.168.1.122:8001/tasks")

        assertTrue(RewardLabNetworkConfig.isOriginAllowed(matching, base))
        assertFalse(RewardLabNetworkConfig.isOriginAllowed(wrongPort, base))
        assertFalse(RewardLabNetworkConfig.isOriginAllowed(wrongHost, base))
        assertFalse(RewardLabNetworkConfig.isOriginAllowed(wrongScheme, base))
    }

    @Test
    fun testDomVerificationParsingReviewed() {
        val jsonStr = """
            {
                "found": true,
                "statusText": "Reviewed",
                "hasSuccessAlert": true,
                "buttonDisabled": true,
                "isPending": false,
                "isReviewed": true
            }
        """.trimIndent()

        val obj = JSONObject(jsonStr)
        val result = DomVerificationResult(
            found = obj.getBoolean("found"),
            statusText = obj.getString("statusText"),
            hasSuccessAlert = obj.getBoolean("hasSuccessAlert"),
            buttonDisabled = obj.getBoolean("buttonDisabled"),
            isPending = obj.getBoolean("isPending"),
            isReviewed = obj.getBoolean("isReviewed")
        )

        assertTrue(result.found)
        assertEquals("Reviewed", result.statusText)
        assertTrue(result.hasSuccessAlert)
        assertTrue(result.buttonDisabled)
        assertFalse(result.isPending)
        assertTrue(result.isReviewed)
    }

    @Test
    fun testDomVerificationParsingPending() {
        val jsonStr = """
            {
                "found": true,
                "statusText": "Pending",
                "hasSuccessAlert": false,
                "buttonDisabled": false,
                "isPending": true,
                "isReviewed": false
            }
        """.trimIndent()

        val obj = JSONObject(jsonStr)
        val result = DomVerificationResult(
            found = obj.getBoolean("found"),
            statusText = obj.getString("statusText"),
            hasSuccessAlert = obj.getBoolean("hasSuccessAlert"),
            buttonDisabled = obj.getBoolean("buttonDisabled"),
            isPending = obj.getBoolean("isPending"),
            isReviewed = obj.getBoolean("isReviewed")
        )

        assertTrue(result.found)
        assertEquals("Pending", result.statusText)
        assertFalse(result.hasSuccessAlert)
        assertFalse(result.buttonDisabled)
        assertTrue(result.isPending)
        assertFalse(result.isReviewed)
    }

    @Test
    fun testDomVerificationParsingNotFound() {
        val jsonStr = """
            {
                "found": false,
                "errorMessage": "TASK_STATUS_ELEMENT_MISSING"
            }
        """.trimIndent()

        val obj = JSONObject(jsonStr)
        val result = DomVerificationResult(
            found = obj.getBoolean("found"),
            errorMessage = obj.optString("errorMessage")
        )

        assertFalse(result.found)
        assertEquals("TASK_STATUS_ELEMENT_MISSING", result.errorMessage)
    }
}
