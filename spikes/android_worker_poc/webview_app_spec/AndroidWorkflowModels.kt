package org.adbap.android.worker

enum class OutcomeType {
    STATE_VERIFIED,
    OUTCOME_UNCONFIRMED,
    READ_ONLY_VERIFIED,
    AUTH_REQUIRED,
    NAVIGATION_FAILED,
    ELEMENT_NOT_FOUND,
    STATE_MISMATCH,
    PROVIDER_REJECTED,
    STOPPED_BY_OPERATOR,
    EXECUTION_FAILED
}

enum class StepStatus {
    PENDING,
    RUNNING,
    SUCCESS,
    FAILED,
    SKIPPED
}

data class StepExecutionResult(
    val stepId: String,
    val action: String,
    val status: StepStatus,
    val durationMs: Long = 0L,
    val detail: String = "",
    val verified: Boolean = false
)

data class JobExecutionResult(
    val workflowId: String,
    val targetUrl: String,
    val outcomeType: OutcomeType,
    val verifiedMutations: List<String> = emptyList(),
    val unverifiedMutations: List<String> = emptyList(),
    val durationSeconds: Double = 0.0,
    val stepResults: List<StepExecutionResult> = emptyList(),
    val summary: String = ""
)

data class DomVerificationResult(
    val found: Boolean,
    val text: String? = null,
    val statusText: String? = null,
    val hasSuccessAlert: Boolean = false,
    val buttonDisabled: Boolean = false,
    val isPending: Boolean = false,
    val isReviewed: Boolean = false,
    val errorMessage: String? = null
)
