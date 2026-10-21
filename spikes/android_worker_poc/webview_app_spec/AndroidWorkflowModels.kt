package org.adbap.android.worker

/**
 * Data models for ADBAP Workflow DSL v1 execution in Android WebView runtime.
 */
data class AndroidWorkflowDefinition(
    val id: String,
    val provider: String,
    val schemaVersion: Int = 1,
    val description: String? = null,
    val steps: List<AndroidWorkflowStep>
)

data class AndroidWorkflowStep(
    val id: String,
    val action: String,
    val path: String? = null,
    val url: String? = null,
    val target: String? = null,
    val sideEffect: Boolean = false,
    val verifiesStepIds: List<String>? = null,
    val reason: String? = null,
    val timeoutSeconds: Int = 10
)

enum class AndroidStepStatus {
    PENDING,
    RUNNING,
    SUCCESS,
    FAILED,
    SKIPPED
}

data class AndroidStepExecutionResult(
    val stepId: String,
    val action: String,
    val status: AndroidStepStatus,
    val isMutating: Boolean = false,
    val durationMs: Long = 0,
    val errorMessage: String? = null
)

data class AndroidJobExecutionResult(
    val workflowId: String,
    val executionId: String,
    val accountId: String,
    val status: String,
    val outcomeType: String,
    val verifiedMutations: List<String>,
    val unverifiedMutations: List<String>,
    val errorCode: String?,
    val errorMessage: String?,
    val requiresVerification: Boolean,
    val durationSeconds: Double,
    val steps: List<AndroidStepExecutionResult>
)
