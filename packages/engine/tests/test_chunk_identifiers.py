"""Identifier splitting for keyword search, and token counting."""

from __future__ import annotations

import pytest

from sensei_engine.chunk.identifiers import index_terms, split_identifier, words
from sensei_engine.chunk.tokens import count_each, count_tokens


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        # camelCase and PascalCase
        ("getUserByID", "getUserByID get user by id"),
        ("getUserById", "getUserById get user by id"),
        ("readItems", "readItems read items"),
        ("UserController", "UserController user controller"),
        ("aB", "aB a b"),
        ("getX", "getX get x"),
        ("isValid2", "isValid2 is valid2"),
        # acronyms
        ("HTTPServer", "HTTPServer http server"),
        ("XMLHttpRequest", "XMLHttpRequest xml http request"),
        ("IOError", "IOError io error"),
        ("MyHTTPServer", "MyHTTPServer my http server"),
        ("parseURL", "parseURL parse url"),
        ("ABCDef", "ABCDef abc def"),
        ("OAuth2PasswordBearer", "OAuth2PasswordBearer o auth2 password bearer"),
        ("getHTTP2Client", "getHTTP2Client get http2 client"),
        ("UTF8Decoder", "UTF8Decoder utf8 decoder"),
        # snake_case, SCREAMING_CASE, kebab-case
        ("get_user_by_id", "get_user_by_id get user by id"),
        ("read_items", "read_items read items"),
        ("MAX_RETRY_COUNT", "MAX_RETRY_COUNT max retry count"),
        ("HTTP_200_OK", "HTTP_200_OK http 200 ok"),
        ("user-profile", "user-profile user profile"),
        ("btn-primary-lg", "btn-primary-lg btn primary lg"),
        ("__init__", "__init__ init"),
        ("_private_helper", "_private_helper private helper"),
        ("snake_case_2", "snake_case_2 snake case 2"),
        ("mixed_camelCase", "mixed_camelCase mixed camel case"),
        # digits
        ("base64Encode", "base64Encode base64 encode"),
        ("md5Hash", "md5Hash md5 hash"),
        ("i18n", "i18n"),
        ("sha256sum", "sha256sum"),
        ("Ipv4Address", "Ipv4Address ipv4 address"),
        ("v2Api", "v2Api v2 api"),
        ("2fa_code", "2fa_code 2fa code"),
        ("error404Page", "error404Page error404 page"),
        # dotted paths and other separators
        ("os.path.join", "os.path.join os path join"),
        ("React.useState", "React.useState react use state"),
        ("app.api.routes.items", "app.api.routes.items app api routes items"),
        ("$store", "$store store"),
        ("api/v1/users", "api/v1/users api v1 users"),
        # single words come back as they are
        ("user", "user"),
        ("User", "User"),
        ("ID", "ID"),
        ("x", "x"),
        # other scripts
        ("überCool", "überCool über cool"),
        ("名前", "名前"),
        # nothing to split
        ("", ""),
        ("___", "___"),
    ],
)
def test_split_identifier(token: str, expected: str) -> None:
    assert split_identifier(token) == expected


def test_words_are_lowercased_and_in_order() -> None:
    assert words("getUserByID") == ["get", "user", "by", "id"]
    assert words("a.b-c_d") == ["a", "b", "c", "d"]


def test_index_terms_join_each_identifier_and_add_its_words() -> None:
    text = "def get_user(user_id):\n    return self.session.getUserById(user_id) or User\n"
    assert index_terms(text) == (
        "def getuser get user userid user id return self session "
        "getuserbyid get user by id userid user id or user"
    )
    assert index_terms("backend/app/models.py: 404") == "backend app models py 404"
    assert index_terms("x = y + 1; __init__") == "x y 1 init"
    assert index_terms("blob " + "A" * 200) == "blob"  # encoded data isn't a term
    assert index_terms("") == ""


def test_tokens_are_counted_with_cl100k() -> None:
    assert count_tokens("") == 0
    assert count_tokens("hello world") == 2
    assert count_tokens("<|endoftext|>") > 1  # special tokens are plain text here
    assert count_each(["hello world", "", "x"]) == [2, 0, 1]
    assert count_each([]) == []
