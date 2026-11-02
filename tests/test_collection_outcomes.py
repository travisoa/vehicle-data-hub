from miit_gonggao.collection_outcomes import model_outcome, product_outcome


def test_detail_and_pdf_are_separate_real_outcomes():
    assert product_outcome(tag="Z", pdf_verified=False, detail_verified=True) == "detail_verified_pdf_pending"
    assert product_outcome(tag="Z", pdf_verified=True, detail_verified=True) == "pdf_and_detail_verified"
    assert product_outcome(tag="Z", pdf_verified=False, detail_verified=False, detail_failed=True) == "detail_retry"
    assert product_outcome(tag="Z", pdf_verified=False, detail_verified=True, pdf_failed=True) == "pdf_retry"
    assert product_outcome(tag="D", pdf_verified=False, detail_verified=False, pdf_failed=True) == "user_excluded"
    assert product_outcome(tag="D", pdf_verified=True, detail_verified=True) == "pdf_and_detail_verified"


def test_models_require_every_whole_product_to_reach_the_same_phase():
    assert model_outcome([]) == "no_whole_vehicle"
    assert model_outcome(["pdf_and_detail_verified", "detail_verified_pdf_pending"]) == "details_verified_pdf_pending"
    assert model_outcome(["pdf_and_detail_verified", "pending"]) == "pending"
    assert model_outcome(["pdf_and_detail_verified", "detail_retry"]) == "detail_retry"
    assert model_outcome(["pdf_and_detail_verified", "pdf_and_detail_verified"]) == "pdf_and_detail_verified"
