use axum::{body::to_bytes, http::StatusCode};
use serde_json::Value;

use super::replacement_test_support::{
    OTHER_SESSION_TOKEN, ReplacementFixture, SESSION_TOKEN, SOURCE_ID, multipart_file,
    multipart_file_with_tables,
};

#[tokio::test]
async fn replaces_compatible_file_when_posting_to_public_endpoint() {
    // Given: a real migrated SQLite database, an authenticated analyst, and two configured tables.
    let fixture = ReplacementFixture::new().await;
    fixture.seed_source().await;
    let original_tables = fixture.table_states().await;
    let body = multipart_file("replacement.csv", b"id,amount\n1,120\n2,240\n3,360\n");

    // When: the client replaces the source through the public multipart endpoint.
    let response = fixture.request(SOURCE_ID, SESSION_TOKEN, body).await;

    // Then: source/table identities survive, metadata changes, and each cache is invalidated once.
    let status = response.status();
    let response_body = to_bytes(response.into_body(), 1_048_576).await.unwrap();
    assert_eq!(
        status,
        StatusCode::OK,
        "{}",
        String::from_utf8_lossy(&response_body)
    );
    let payload: Value = serde_json::from_slice(&response_body).unwrap();
    assert_eq!(payload["id"], SOURCE_ID);
    assert_eq!(payload["originalFilename"], "replacement.csv");
    assert_eq!(payload["rowCount"], 3);

    let replaced_tables = fixture.table_states().await;
    assert_eq!(replaced_tables.len(), original_tables.len());
    for (before, after) in original_tables.iter().zip(&replaced_tables) {
        assert_eq!(after.id, before.id);
        assert_eq!(after.name, before.name);
        assert_eq!(after.is_default, before.is_default);
        assert_eq!(after.config_version, before.config_version + 1);
        assert_eq!(after.cache_status, "pending");
        assert_eq!(after.cache_key, None);
        assert_eq!(after.cache_error, None);
        assert_eq!(after.row_count, 3);
        assert_eq!(after.schema_json, before.schema_json);
    }
    assert!(!fixture.cache_path('a').exists());
    assert!(!fixture.cache_path('b').exists());
    assert!(fixture.temporary_artifacts().is_empty());
}

#[tokio::test]
async fn restores_old_file_and_metadata_when_transaction_fails_after_file_swap() {
    // Given: a compatible replacement and a trigger that rejects the metadata update.
    let fixture = ReplacementFixture::new().await;
    fixture.seed_source().await;
    fixture.force_data_source_update_failure().await;
    let metadata_before = fixture.source_state().await;
    let tables_before = fixture.table_states().await;
    let old_bytes = std::fs::read(&metadata_before.stored_path).unwrap();
    let body = multipart_file("replacement.csv", b"id,amount\n1,120\n2,240\n");

    // When: the endpoint swaps in the staged file and the SQL transaction fails.
    let response = fixture.request(SOURCE_ID, SESSION_TOKEN, body).await;

    // Then: the endpoint fails and restores every durable file and metadata value.
    assert!(response.status().is_server_error());
    let metadata_after = fixture.source_state().await;
    assert_eq!(metadata_after, metadata_before);
    assert_eq!(fixture.table_states().await, tables_before);
    assert_eq!(
        std::fs::read(&metadata_after.stored_path).unwrap(),
        old_bytes
    );
    assert!(fixture.temporary_artifacts().is_empty());
}

#[tokio::test]
async fn returns_success_when_post_commit_cache_cleanup_fails() {
    // Given: a compatible replacement whose old cache path cannot be removed as a file.
    let fixture = ReplacementFixture::new().await;
    fixture.seed_source().await;
    fixture.force_cache_cleanup_failure('a');
    let body = multipart_file("replacement.csv", b"id,amount\n1,120\n2,240\n");

    // When: replacement commits before cache cleanup encounters the filesystem error.
    let response = fixture.request(SOURCE_ID, SESSION_TOKEN, body).await;

    // Then: the committed replacement is reported as successful and its backup is gone.
    assert_eq!(response.status(), StatusCode::OK);
    assert_eq!(
        fixture.source_state().await.original_filename,
        "replacement.csv"
    );
    assert!(fixture.temporary_artifacts().is_empty());
}

#[tokio::test]
async fn preserves_file_and_metadata_when_replacement_schema_mismatches() {
    // Given: an existing source whose stored schema has two named columns.
    let fixture = ReplacementFixture::new().await;
    fixture.seed_source().await;
    let metadata_before = fixture.source_state().await;
    let tables_before = fixture.table_states().await;
    let old_bytes = std::fs::read(&metadata_before.stored_path).unwrap();
    let body = multipart_file("incompatible.csv", b"id,changed,total\n1,2,3\n");

    // When: the uploaded file changes both field names and count.
    let response = fixture.request(SOURCE_ID, SESSION_TOKEN, body).await;

    // Then: validation rejects it before the file swap or metadata transaction.
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    let metadata_after = fixture.source_state().await;
    assert_eq!(metadata_after, metadata_before);
    assert_eq!(fixture.table_states().await, tables_before);
    assert_eq!(
        std::fs::read(&metadata_after.stored_path).unwrap(),
        old_bytes
    );
    assert!(fixture.temporary_artifacts().is_empty());
}

#[tokio::test]
async fn returns_not_found_when_source_belongs_to_another_workspace() {
    // Given: a source in one workspace and an analyst session in another workspace.
    let fixture = ReplacementFixture::new().await;
    fixture.seed_source().await;
    fixture.seed_other_workspace_session().await;
    let metadata_before = fixture.source_state().await;
    let body = multipart_file("replacement.csv", b"id,amount\n1,20\n");

    // When: the other workspace posts to the replacement endpoint.
    let response = fixture.request(SOURCE_ID, OTHER_SESSION_TOKEN, body).await;

    // Then: tenant scoping hides the source and leaves it unchanged.
    assert_eq!(response.status(), StatusCode::NOT_FOUND);
    assert_eq!(fixture.source_state().await, metadata_before);
}

#[tokio::test]
async fn rejects_explicit_tables_multipart_field_for_mvp() {
    // Given: a compatible replacement plus an explicit tables field.
    let fixture = ReplacementFixture::new().await;
    fixture.seed_source().await;
    let metadata_before = fixture.source_state().await;
    let body = multipart_file_with_tables("replacement.csv", b"id,amount\n1,20\n");

    // When: the client requests unsupported explicit table configuration.
    let response = fixture.request(SOURCE_ID, SESSION_TOKEN, body).await;

    // Then: the boundary rejects the request without modifying the source.
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    assert_eq!(fixture.source_state().await, metadata_before);
}
