"""A shared business account must not share visitor tasks or write previews."""
import asyncio
import pytest
from ktg_agent.ktg.client import KTGClient, KTGError
from ktg_agent.sessions.task_tools import owner_from_identity
from ktg_agent.service import QueryService
from ktg_agent.ktg.workorders import _identity_key as workorder_key
from ktg_agent.ktg.inventory import _user_key as inventory_key
from ktg_agent.ktg.dv_repair_writes import _identity_key as repair_key
from ktg_agent.ktg.dv_checkplan_writes import _identity_key as checkplan_key
from ktg_agent.ktg.dv_machinery_writes import _identity_key as machinery_key
from ktg_agent.ktg.dv_subject_writes import _identity_key as subject_key
from ktg_agent.ktg.tm_tooltype_writes import _identity_key as tooltype_key


async def transport(url, headers, params, timeout):
    if headers['Authorization']=='Bearer invalid':
        return {'code':401,'msg':'expired'}
    return {'code':200,'user':{'userId':9000,'userName':'demo','_agent_scope':'untrusted'}}


def identity(token, scoped=True):
    return asyncio.run(KTGClient(base_url='http://mes.test',authorization=f'Bearer {token}',
                                transport=transport,scope_by_token=scoped).get_current_user())


@pytest.mark.parametrize('key',[owner_from_identity,QueryService._identity_key,workorder_key,inventory_key,
                               repair_key,checkplan_key,machinery_key,subject_key,tooltype_key])
def test_visitor_scope_is_consistent_stable_and_separate(key):
    first,second=identity('first'),identity('second')
    assert first['userId']==second['userId']==9000
    assert key(first)==key(identity('first'))==owner_from_identity(first)
    assert key(first)!=key(second)
    assert key(identity('first',False))=='id:9000'


def test_scope_is_not_accepted_before_authentication():
    with pytest.raises(KTGError): identity('invalid')
