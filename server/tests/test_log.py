import io

from loguru import logger

from internal.log import configure_logging


def test_connection_error_names_the_source_but_not_the_password():
    stream = io.StringIO()
    configure_logging(["s3cr3t-db-password"], stream)
    try:
        raise ConnectionError("cannot reach postgresql://reader:s3cr3t-db-password@db.internal:5432/app")
    except ConnectionError:
        logger.exception("Source database is unavailable")
    written = stream.getvalue()
    assert "Source database is unavailable" in written
    assert "db.internal" in written
    assert "s3cr3t-db-password" not in written


def test_bot_token_in_a_bot_api_address_is_cut_out():
    stream = io.StringIO()
    configure_logging([], stream)
    logger.warning("POST https://api.telegram.org/bot123456789:AAFakeTokenValueThatIsLongEnough_xyz/getUpdates failed")
    assert "AAFakeTokenValueThatIsLongEnough_xyz" not in stream.getvalue()


def test_provider_tokens_are_cut_out():
    stream = io.StringIO()
    configure_logging([], stream)
    logger.info("refresh with eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.c2lnbmF0dXJl refused")
    assert "eyJ" not in stream.getvalue()
