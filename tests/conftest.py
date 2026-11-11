import pytest


@pytest.fixture(autouse=True)
def _youtube_db(request):
    """Tests that touch the youtube schema (they take db_session/db_env) get a migrated throwaway database; the others never see one."""
    if {'db_session', 'db_env', 'db_url', 'db_engine'} & set(request.fixturenames): request.getfixturevalue('db_env')
