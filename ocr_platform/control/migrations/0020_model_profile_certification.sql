-- Additive storage for v0.4 model-profile certification provenance.
--
-- v0.3.2 does not expose or enforce these fields. The table intentionally
-- stores only immutable revisions/digests and an auditable risk-acceptance
-- record; it must not contain credentials, private endpoints, OCR content, or
-- customer documents.

CREATE TABLE IF NOT EXISTS model_profile_certifications (
    profile_id VARCHAR(128) PRIMARY KEY
        REFERENCES model_profiles(id) ON DELETE CASCADE,
    enforcement VARCHAR(32) NOT NULL DEFAULT 'off',
    status VARCHAR(32) NOT NULL DEFAULT 'contract_only',
    parser_revision VARCHAR(255),
    parser_digest VARCHAR(255),
    model_revision VARCHAR(255),
    model_digest VARCHAR(255),
    runtime_revision VARCHAR(255),
    runtime_digest VARCHAR(255),
    layout_revision VARCHAR(255),
    layout_digest VARCHAR(255),
    fixture_set_digest VARCHAR(255),
    evidence_digest VARCHAR(255),
    certified_at TIMESTAMPTZ,
    risk_acceptance_json TEXT NOT NULL DEFAULT '{}',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_model_profile_certifications_enforcement
        CHECK (enforcement IN ('off', 'verified', 'certified')),
    CONSTRAINT ck_model_profile_certifications_status
        CHECK (status IN ('contract_only', 'verified', 'certified', 'blocked'))
);

INSERT INTO schema_migrations (version)
VALUES ('0020_model_profile_certification')
ON CONFLICT (version) DO NOTHING;
