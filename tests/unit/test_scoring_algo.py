from lib.scoring_algo import score


def test_perfect_match_returns_high_score():
    jd = "Customer Success Manager with QBR, NRR, and SaaS experience."
    resume = "Customer Success Manager with QBR, NRR, and SaaS experience."
    result = score(jd_text=jd, resume_text=resume)
    assert result["score"] >= 95
    assert result["verdict"] == "apply"
    assert result["missing_terms"] == []


def test_zero_overlap_returns_low_score():
    jd = "Senior Backend Engineer in Rust working on GPU kernels."
    resume = "Pastry chef with 10 years of experience in French cuisine."
    result = score(jd_text=jd, resume_text=resume)
    assert result["score"] < 30
    assert result["verdict"] == "stretch"


def test_borderline_match_yields_tailor_verdict():
    jd = "Customer Success Manager. SaaS. Renewals. NRR. APIs. Python. FastAPI."
    resume = "Customer Success Manager experienced in renewals and APIs."
    result = score(jd_text=jd, resume_text=resume)
    assert 65 <= result["score"] < 90
    assert result["verdict"] == "tailor"
    assert "saas" in [t.lower() for t in result["missing_terms"]]


def test_envelope_shape():
    result = score(jd_text="Python SaaS", resume_text="Python")
    assert set(result.keys()) >= {"score", "verdict", "missing_terms"}
    assert isinstance(result["missing_terms"], list)
