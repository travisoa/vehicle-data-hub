"""Mechanical collection outcomes; callers must verify the underlying evidence first."""
from collections.abc import Iterable


def product_outcome(*, tag: str, pdf_verified: bool, detail_verified: bool,
                    pdf_failed: bool = False, detail_failed: bool = False) -> str:
    """Classify one exact product without treating a detail as a PDF."""
    if pdf_verified and detail_verified:
        return "pdf_and_detail_verified"
    if tag == "D":
        return "user_excluded"
    if pdf_failed:
        return "pdf_retry"
    if detail_failed and not detail_verified:
        return "detail_retry"
    if detail_verified:
        return "detail_verified_pdf_pending"
    return "pending"


def model_outcome(outcomes: Iterable[str]) -> str:
    """Roll up exact whole-vehicle products; chassis scope is handled separately."""
    items = list(outcomes)
    if not items:
        return "no_whole_vehicle"
    if "pdf_retry" in items:
        return "pdf_retry"
    if "detail_retry" in items:
        return "detail_retry"
    if all(item == "pdf_and_detail_verified" for item in items):
        return "pdf_and_detail_verified"
    if all(item in {"pdf_and_detail_verified", "detail_verified_pdf_pending"} for item in items):
        return "details_verified_pdf_pending"
    return "pending"
