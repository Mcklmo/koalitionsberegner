"""Posting an approved reply to Reddit, without ever reaching Reddit.

Every case talks to an ``httpx.MockTransport``; a fixture makes the real
transport raise, so a poster built without one fails here instead of posting.
The credentials are made up. What these pin down: the two requests have the
shape Reddit documents, the bearer token is reused until shortly before it
expires, a failure never carries Reddit's words or the secrets, and a comment
Reddit accepted is never reported as one it did not.
"""

from __future__ import annotations

import base64
import logging
from urllib.parse import parse_qs

import anyio
import httpx
import pytest

from app import reddit
from app.reddit import (
    CREDENTIAL_VARIABLES,
    DisabledRedditPoster,
    FakeRedditPoster,
    HttpxRedditPoster,
    PostedComment,
    RedditCredentials,
    RedditUnavailable,
    missing_credentials,
    poster_from_env,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Anything that tries the real transport fails instead of reaching Reddit."""

    async def refuse(self, request):
        raise AssertionError(f"a test tried to reach {request.url}")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)


CLIENT_SECRET = "made-up-client-secret"
PASSWORD = "made-up-password"
TOKEN = "made-up-bearer"

CREDENTIALS = RedditCredentials(
    client_id="made-up-id",
    client_secret=CLIENT_SECRET,
    username="koalitionsberegner",
    password=PASSWORD,
    user_agent="koalitionsberegner-outreach/0.1 (by u/koalitionsberegner)",
)

ENV = {
    "REDDIT_CLIENT_ID": "made-up-id",
    "REDDIT_CLIENT_SECRET": CLIENT_SECRET,
    "REDDIT_USERNAME": "koalitionsberegner",
    "REDDIT_PASSWORD": PASSWORD,
    "REDDIT_USER_AGENT": "koalitionsberegner-outreach/0.1 (by u/koalitionsberegner)",
}

REPLY = "The seats are split like this: https://koalitionsberegner.moritzmarcus.com/"

#: What Reddit says when a message is echoed somewhere it must not be.
REDDIT_WORDS = "you are doing that too much. try again in 9 minutes."


def token_answer(**overrides) -> httpx.Response:
    body = {"access_token": TOKEN, "token_type": "bearer", "expires_in": 3600, "scope": "*"}
    body.update(overrides)
    return httpx.Response(200, json=body)


def comment_answer(
    name: str = "t1_newone",
    permalink: str = "/r/denmark/comments/abc123/valg/newone/",
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "json": {
                "errors": [],
                "data": {
                    "things": [
                        {"kind": "t1", "data": {"name": name, "permalink": permalink}}
                    ]
                },
            }
        },
    )


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Reddit:
    """A scripted Reddit: answers per endpoint, and every request it saw."""

    def __init__(self, *, token=None, comment=None):
        self.token = token or (lambda request: token_answer())
        self.comment = comment or (lambda request: comment_answer())
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == reddit.TOKEN_URL:
            return self.token(request)
        if str(request.url) == reddit.COMMENT_URL:
            return self.comment(request)
        raise AssertionError(f"unexpected request to {request.url}")

    def to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]


def poster(fake: Reddit, clock=None) -> HttpxRedditPoster:
    return HttpxRedditPoster(
        CREDENTIALS,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake)),
        clock=clock or FakeClock(),
    )


def form(request: httpx.Request) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(request.content.decode()).items()}


# --- the two requests -------------------------------------------------------

async def test_a_reply_logs_in_as_the_script_app_and_comments_under_the_parent():
    fake = Reddit()

    posted = await poster(fake).comment("t3_abc123", REPLY)

    assert posted == PostedComment(
        fullname="t1_newone",
        url="https://www.reddit.com/r/denmark/comments/abc123/valg/newone/",
    )
    login, comment = fake.requests
    assert str(login.url) == "https://www.reddit.com/api/v1/access_token"
    assert login.headers["authorization"] == "Basic " + base64.b64encode(
        f"made-up-id:{CLIENT_SECRET}".encode()
    ).decode()
    assert form(login) == {
        "grant_type": "password",
        "username": "koalitionsberegner",
        "password": PASSWORD,
    }
    assert str(comment.url) == "https://oauth.reddit.com/api/comment"
    assert comment.headers["authorization"] == f"bearer {TOKEN}"
    assert form(comment) == {"api_type": "json", "thing_id": "t3_abc123", "text": REPLY}
    for request in fake.requests:
        assert request.headers["user-agent"] == CREDENTIALS.user_agent


async def test_a_reply_to_a_comment_names_the_comment_as_parent():
    fake = Reddit()

    await poster(fake).comment("t1_xyz9", REPLY)

    assert form(fake.to(reddit.COMMENT_URL)[0])["thing_id"] == "t1_xyz9"


@pytest.mark.parametrize(
    "thing_id",
    ["", "abc123", "t2_abc123", "t5_denmark", "T3_ABC", "t3_abc&text=spam", "t3_abc\n", None],
)
async def test_a_parent_that_is_not_a_post_or_comment_is_refused_before_any_request(thing_id):
    fake = Reddit()

    with pytest.raises(ValueError):
        await poster(fake).comment(thing_id, REPLY)

    assert fake.requests == []


@pytest.mark.parametrize("text", ["", "   \n", None])
async def test_an_empty_reply_is_refused_before_any_request(text):
    fake = Reddit()

    with pytest.raises(ValueError):
        await poster(fake).comment("t3_abc123", text)

    assert fake.requests == []


# --- the bearer token -------------------------------------------------------

async def test_the_token_is_reused_until_shortly_before_it_expires():
    fake, clock = Reddit(), FakeClock()
    subject = poster(fake, clock)

    await subject.comment("t3_one", REPLY)
    clock.now += 3600 - reddit.TOKEN_MARGIN_SECONDS - 1
    await subject.comment("t3_two", REPLY)
    assert len(fake.to(reddit.TOKEN_URL)) == 1

    clock.now += 1
    await subject.comment("t3_three", REPLY)
    assert len(fake.to(reddit.TOKEN_URL)) == 2


async def test_a_token_without_a_lifetime_is_kept_for_reddits_documented_hour():
    fake, clock = Reddit(token=lambda r: token_answer(expires_in=None)), FakeClock()
    subject = poster(fake, clock)

    await subject.comment("t3_one", REPLY)
    clock.now += reddit.DEFAULT_TOKEN_SECONDS - reddit.TOKEN_MARGIN_SECONDS - 1
    await subject.comment("t3_two", REPLY)

    assert len(fake.to(reddit.TOKEN_URL)) == 1


async def test_concurrent_replies_share_one_login():
    fake = Reddit()
    subject = poster(fake)

    async with anyio.create_task_group() as group:
        for n in range(4):
            group.start_soon(subject.comment, f"t3_post{n}", REPLY)

    assert len(fake.to(reddit.TOKEN_URL)) == 1
    assert len(fake.to(reddit.COMMENT_URL)) == 4


async def test_a_token_reddit_stops_accepting_is_replaced_once():
    answers = iter([httpx.Response(401), comment_answer()])
    fake = Reddit(comment=lambda r: next(answers))

    posted = await poster(fake).comment("t3_abc123", REPLY)

    assert posted.fullname == "t1_newone"
    assert len(fake.to(reddit.TOKEN_URL)) == 2
    assert len(fake.to(reddit.COMMENT_URL)) == 2


async def test_a_second_refusal_of_a_fresh_token_is_not_retried_again():
    fake = Reddit(comment=lambda r: httpx.Response(401))

    with pytest.raises(RedditUnavailable) as caught:
        await poster(fake).comment("t3_abc123", REPLY)

    assert str(caught.value) == reddit.REFUSED
    assert caught.value.maybe_posted is False
    assert len(fake.to(reddit.COMMENT_URL)) == 2


# --- a login that does not work ---------------------------------------------

@pytest.mark.parametrize(
    "answer",
    [
        # A wrong password: Reddit says so under a 200.
        lambda r: httpx.Response(200, json={"error": "invalid_grant"}),
        lambda r: httpx.Response(401, json={"message": "Unauthorized", "error": 401}),
        lambda r: httpx.Response(200, text="<html>not json</html>"),
        lambda r: token_answer(access_token=""),
        lambda r: token_answer(token_type="mac"),
        lambda r: token_answer(scope="identity read"),
    ],
)
async def test_a_login_reddit_does_not_accept_posts_nothing(answer):
    fake = Reddit(token=answer)

    with pytest.raises(RedditUnavailable) as caught:
        await poster(fake).comment("t3_abc123", REPLY)

    assert str(caught.value) == reddit.LOGIN_FAILED
    assert caught.value.maybe_posted is False
    assert fake.to(reddit.COMMENT_URL) == []


async def test_a_rate_limited_login_says_so():
    fake = Reddit(token=lambda r: httpx.Response(429))

    with pytest.raises(RedditUnavailable) as caught:
        await poster(fake).comment("t3_abc123", REPLY)

    assert str(caught.value) == reddit.RATE_LIMITED


async def test_an_unreachable_login_is_unreachable_and_certainly_not_posted():
    def down(request):
        raise httpx.ConnectError("no route", request=request)

    with pytest.raises(RedditUnavailable) as caught:
        await poster(Reddit(token=down)).comment("t3_abc123", REPLY)

    assert str(caught.value) == reddit.UNREACHABLE
    assert caught.value.maybe_posted is False


# --- a comment that does not work -------------------------------------------

def reddit_error(code: str) -> httpx.Response:
    return httpx.Response(
        200, json={"json": {"errors": [[code, REDDIT_WORDS, "ratelimit"]]}}
    )


async def test_a_rate_limit_inside_a_200_is_a_rate_limit_not_a_success(caplog):
    fake = Reddit(comment=lambda r: reddit_error("RATELIMIT"))

    with caplog.at_level(logging.DEBUG, logger="app.reddit"):
        with pytest.raises(RedditUnavailable) as caught:
            await poster(fake).comment("t3_abc123", REPLY)

    assert str(caught.value) == reddit.RATE_LIMITED
    assert caught.value.maybe_posted is False
    assert "RATELIMIT" in caplog.text, "the code is kept for the log"
    assert REDDIT_WORDS not in caplog.text, "Reddit's message is not"


async def test_any_other_error_inside_a_200_is_a_refusal():
    fake = Reddit(comment=lambda r: reddit_error("THREAD_LOCKED"))

    with pytest.raises(RedditUnavailable) as caught:
        await poster(fake).comment("t3_abc123", REPLY)

    assert str(caught.value) == reddit.REFUSED


@pytest.mark.parametrize(
    ("status", "message", "maybe_posted"),
    [
        (403, reddit.REFUSED, False),
        (404, reddit.REFUSED, False),
        (429, reddit.RATE_LIMITED, False),
        # A gateway error can arrive after the comment was made.
        (500, reddit.UNCERTAIN, True),
        (502, reddit.UNCERTAIN, True),
        (504, reddit.UNCERTAIN, True),
    ],
)
async def test_a_refused_comment_says_whether_it_might_have_posted(status, message, maybe_posted):
    fake = Reddit(comment=lambda r: httpx.Response(status, text=REDDIT_WORDS))

    with pytest.raises(RedditUnavailable) as caught:
        await poster(fake).comment("t3_abc123", REPLY)

    assert str(caught.value) == message
    assert caught.value.maybe_posted is maybe_posted


@pytest.mark.parametrize(
    ("error", "message", "maybe_posted"),
    [
        (httpx.ConnectError, reddit.UNREACHABLE, False),
        (httpx.ConnectTimeout, reddit.UNREACHABLE, False),
        (httpx.ReadTimeout, reddit.UNCERTAIN, True),
        (httpx.RemoteProtocolError, reddit.UNCERTAIN, True),
    ],
)
async def test_a_lost_answer_is_uncertain_but_an_unsent_request_is_not(error, message, maybe_posted):
    def fail(request):
        raise error("gone", request=request)

    with pytest.raises(RedditUnavailable) as caught:
        await poster(Reddit(comment=fail)).comment("t3_abc123", REPLY)

    assert str(caught.value) == message
    assert caught.value.maybe_posted is maybe_posted


# --- a comment Reddit accepted ----------------------------------------------

@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(200, json={"json": {"errors": [], "data": {}}}),
        httpx.Response(200, json={"json": {"errors": []}}),
        httpx.Response(200, json={}),
        httpx.Response(200, text="ok"),
    ],
)
async def test_an_accepted_comment_with_an_odd_answer_is_still_posted(answer):
    """Raising here would invite the owner to retry, and post twice."""
    posted = await poster(Reddit(comment=lambda r: answer)).comment("t3_abc123", REPLY)

    assert posted == PostedComment(fullname=None, url=None)


@pytest.mark.parametrize(
    "permalink",
    [
        "https://evil.example/r/denmark/comments/x/",
        "//evil.example/r/denmark/comments/x/",
        "/r/denmark/comments/x/ <script>",
        "/user/somebody/",
        "javascript:alert(1)",
    ],
)
async def test_a_permalink_that_is_not_a_reddit_path_is_dropped(permalink):
    """The URL is stored and shown to the owner; Reddit's answer is data."""
    fake = Reddit(comment=lambda r: comment_answer(permalink=permalink))

    posted = await poster(fake).comment("t3_abc123", REPLY)

    assert posted.url is None
    assert posted.fullname == "t1_newone"


async def test_a_name_that_is_not_a_comment_fullname_is_dropped():
    fake = Reddit(comment=lambda r: comment_answer(name="t1_x\nforged log line"))

    posted = await poster(fake).comment("t3_abc123", REPLY)

    assert posted.fullname is None


# --- secrets stay secret ----------------------------------------------------

async def test_no_secret_reaches_a_log_line_or_an_error(caplog):
    answers = [
        lambda r: httpx.Response(200, json={"error": "invalid_grant"}),
        lambda r: httpx.Response(401, text=f"bad {PASSWORD}"),
    ]
    messages = []
    with caplog.at_level(logging.DEBUG):
        for answer in answers:
            with pytest.raises(RedditUnavailable) as caught:
                await poster(Reddit(token=answer)).comment("t3_abc123", REPLY)
            messages.append(str(caught.value))
        await poster(Reddit()).comment("t3_abc123", REPLY)

    for secret in (CLIENT_SECRET, PASSWORD, TOKEN):
        assert secret not in caplog.text
        assert all(secret not in message for message in messages)
    assert REPLY not in caplog.text, "the reply text is not logged either"


def test_credentials_and_poster_do_not_print_their_secrets():
    shown = repr(CREDENTIALS) + repr(HttpxRedditPoster(CREDENTIALS))

    assert CLIENT_SECRET not in shown
    assert PASSWORD not in shown


# --- configuration ----------------------------------------------------------

def test_all_five_variables_switch_posting_on():
    subject = poster_from_env(ENV)

    assert isinstance(subject, HttpxRedditPoster)
    assert subject.enabled is True
    assert missing_credentials(ENV) == []


@pytest.mark.parametrize("name", CREDENTIAL_VARIABLES)
async def test_any_one_missing_variable_leaves_posting_off(name, caplog):
    env = {**ENV, name: "   "}

    with caplog.at_level(logging.WARNING, logger="app.reddit"):
        subject = poster_from_env(env)

    assert isinstance(subject, DisabledRedditPoster)
    assert subject.enabled is False
    assert subject.missing == (name,)
    assert name in caplog.text
    assert CLIENT_SECRET not in caplog.text and PASSWORD not in caplog.text
    with pytest.raises(RedditUnavailable) as caught:
        await subject.comment("t3_abc123", REPLY)
    assert str(caught.value) == reddit.NOT_CONFIGURED


def test_nothing_set_is_off_without_a_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="app.reddit"):
        subject = poster_from_env({})

    assert isinstance(subject, DisabledRedditPoster)
    assert subject.missing == CREDENTIAL_VARIABLES
    assert caplog.text == ""


def test_the_environment_is_read_when_none_is_given(monkeypatch):
    for name in CREDENTIAL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert isinstance(poster_from_env(), DisabledRedditPoster)

    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    assert isinstance(poster_from_env(), HttpxRedditPoster)


# --- the fake ---------------------------------------------------------------

async def test_the_fake_remembers_what_it_would_have_posted():
    fake = FakeRedditPoster()

    first = await fake.comment("t3_abc123", REPLY)
    second = await fake.comment("t1_def456", "another")

    assert fake.posted == [("t3_abc123", REPLY), ("t1_def456", "another")]
    assert first.fullname == "t1_fake1" and second.fullname == "t1_fake2"
    assert first.url and first.url.startswith("https://www.reddit.com/r/")


async def test_the_fake_can_fail_and_then_posts_nothing():
    fake = FakeRedditPoster(fail_with=RedditUnavailable(reddit.UNCERTAIN, maybe_posted=True))

    with pytest.raises(RedditUnavailable) as caught:
        await fake.comment("t3_abc123", REPLY)

    assert caught.value.maybe_posted is True
    assert fake.posted == []


async def test_the_fake_refuses_what_the_real_poster_refuses():
    with pytest.raises(ValueError):
        await FakeRedditPoster().comment("t2_someone", REPLY)
