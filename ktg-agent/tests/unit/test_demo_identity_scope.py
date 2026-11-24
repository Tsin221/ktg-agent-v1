"""A shared business account must not share visitor tasks."""
import asyncio
from collections.abc import Mapping
import pytest
from ktg_agent.core.messages import JsonObject
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.sessions.task_tools import owner_from_identity


async def transport(url: str, headers: Mapping[str, str], params: Mapping[str, object], timeout: float) -> object:
    if headers['Authorization']=='Bearer invalid':
        return {'code':401,'msg':'expired'}
    return {'code':200,'user':{'userId':9000,'userName':'demo','_agent_scope':'untrusted'}}


def identity(token: str, scoped: bool = True) -> JsonObject:
    return asyncio.run(KTGClient(base_url='http://mes.test',authorization=f'Bearer {token}',
                                transport=transport,scope_by_token=scoped).get_current_user())


def test_visitor_scope_is_consistent_stable_and_separate() -> None:
    key = owner_from_identity
    first,second=identity('first'),identity('second')
    assert first['userId']==second['userId']==9000
    assert key(first)==key(identity('first'))==owner_from_identity(first)
    assert key(first)!=key(second)
    assert key(identity('first',False))=='id:9000'


def test_scope_is_not_accepted_before_authentication() -> None:
    with pytest.raises(KTGError): identity('invalid')
