import asyncio
import sqlite3

import pytest

from src.models import Settings, State
from src.notifications import Notifications
from src.storage import Storage
from src.telegram import Recipients, Receiver, TelegramFailure, scope_for


def settings(**extra):
    return Settings(telegram_enabled=True, telegram_token='TEST_ONLY', telegram_polling_enabled=True,
                    ingress_url='https://ha.example.invalid/hassio/ingress/local_barcelona_cita',
                    ha_notifications=False, **extra)


def update(uid, chat, text, kind='private'):
    return {'update_id':uid,'message':{'chat':{'id':chat,'type':kind,'first_name':'TEST USER'},'text':text}}


def test_private_invitation_one_use_and_durable_offset(tmp_path):
    db=Storage(tmp_path/'data');s=settings();r=Recipients(db)
    code=r.invite(s)
    assert r.process(s,update(1,1001,'/start bad'),lambda:'PRIVATE_STATUS')[1].startswith('Для подписки')
    assert r.process(s,update(2,-1001,'/start '+code,'group'),lambda:'PRIVATE_STATUS') is None
    assert not r.effective(s)
    assert 'включена' in r.process(s,update(3,1001,'/start '+code),lambda:'PRIVATE_STATUS')[1]
    assert not r.process(s,update(3,1001,'/stop'),lambda:'PRIVATE_STATUS')
    assert r.effective(s)==['1001']
    assert 'Для подписки' in r.process(s,update(4,1002,'/start '+code),lambda:'PRIVATE_STATUS')[1]
    db.close()
    db=Storage(tmp_path/'data');r=Recipients(db)
    assert r.effective(s)==['1001'] and r.offset(scope_for(s.telegram_token))==5
    assert r.process(s,update(4,1001,'/stop'),lambda:'PRIVATE_STATUS') is None
    assert r.process(s,update(5,1001,'/status'),lambda:'PRIVATE_STATUS')[1]=='PRIVATE_STATUS'
    assert r.process(s,update(6,1002,'/status'),lambda:'PRIVATE_STATUS')[1]!='PRIVATE_STATUS'
    db.close()


def test_limit_expiry_optout_legacy_and_token_scope(controller):
    s=settings(telegram_chat_ids=['1001']);r=Recipients(controller.storage,clock=lambda:100)
    assert r.effective(s)==['1001']
    code=r.invite(s)
    r.process(s,update(1,1002,'/start '+code),lambda:'status')
    third=r.invite(s)
    assert 'предел' in r.process(s,update(2,1003,'/start '+third),lambda:'status')[1]
    controller.monitoring=True
    r.process(s,update(3,1001,'/stop'),lambda:'status')
    assert controller.monitoring and r.effective(s)==['1002']
    assert s.telegram_chat_ids==['1001']
    r.migrate(s)
    assert r.effective(s)==['1002']
    expired=r.invite(s);r.clock=lambda:701
    assert 'Для подписки' in r.process(s,update(4,1003,'/start '+expired),lambda:'status')[1]
    changed=s.model_copy(update={'telegram_token':'DIFFERENT_TEST','telegram_chat_ids':[]})
    assert not r.effective(changed)
    assert '1001' not in str(controller.storage.events())


def test_invite_and_update_cursor_are_atomic(controller):
    s=settings();r=Recipients(controller.storage);code=r.invite(s)
    controller.storage.db.execute("CREATE TEMP TRIGGER fail_cursor BEFORE INSERT ON telegram_offsets BEGIN SELECT RAISE(ABORT,'TEST_FAILURE'); END")
    with pytest.raises(sqlite3.IntegrityError):
        r.process(s,update(1,1001,'/start '+code),lambda:'status')
    assert not r.effective(s) and r.offset(scope_for(s.telegram_token))==0
    controller.storage.db.execute('DROP TRIGGER fail_cursor')
    r.process(s,update(1,1001,'/start '+code),lambda:'status')
    assert r.effective(s)==['1001']


async def test_fanout_two_subscribers_is_parallel_and_private(controller,monkeypatch):
    s=settings();n=Notifications(lambda:s,controller.storage)
    for uid,chat in enumerate([1001,1002],1):
        code=n.recipients.invite(s)
        n.recipients.process(s,update(uid,chat,'/start '+code),lambda:'status')
    sent=[];both=asyncio.Event()
    async def post(url,payload,**kwargs):
        sent.append(payload)
        if len(sent)==2:both.set()
        await asyncio.wait_for(both.wait(),1)
    monkeypatch.setattr(n,'post',post)
    n.emit(controller.profiles[0],State.POSSIBLE_SLOTS)
    profile,text=n.queue.get_nowait()
    await n.send(profile,text)
    assert {x['chat_id'] for x in sent}=={'1001','1002'}
    assert all(x['reply_markup']['inline_keyboard'][0][0]['url']==s.ingress_url for x in sent)
    assert all(x['reply_markup']['inline_keyboard'][0][0]['text']=='🖥 Открыть рабочий стол' for x in sent)
    assert controller.profiles[0].nie not in text and 'не зарезервировано' in text


@pytest.mark.parametrize('fault',['TELEGRAM_WEBHOOK_CONFLICT','TELEGRAM_POLLING_CONFLICT'])
async def test_conflicts_stop_receiver_not_monitoring(controller,fault):
    s=settings();r=Recipients(controller.storage)
    receiver=Receiver(lambda:s,controller.storage,r,lambda:'status')
    calls=[]
    async def api(token,method,payload):
        calls.append(method)
        if method=='getWebhookInfo':return {'url':'https://example.invalid/hook' if fault=='TELEGRAM_WEBHOOK_CONFLICT' else ''}
        raise TelegramFailure('TELEGRAM_POLLING_CONFLICT')
    receiver.api=api
    controller.monitoring=True
    receiver.start();task=receiver.task;receiver.start();assert receiver.task is task
    for _ in range(50):
        if receiver.state==fault:break
        await asyncio.sleep(.01)
    await receiver.close()
    assert receiver.state==fault and controller.monitoring
    assert controller.storage.get('telegram_fault_'+scope_for(s.telegram_token))==fault
    if fault=='TELEGRAM_WEBHOOK_CONFLICT':assert calls==['getWebhookInfo']


async def test_web_recipient_admin_routes_require_csrf(controller):
    from aiohttp.test_utils import TestClient,TestServer
    from src.web import create_app
    controller.settings=settings()
    controller.notifications=Notifications(lambda:controller.settings,controller.storage)
    async with TestClient(TestServer(create_app(controller,trusted_peer='127.0.0.1'))) as client:
        assert (await client.post('/api/telegram',json={'action':'invite'})).status==403
        csrf=(await(await client.get('/api/status')).json())['csrf']
        headers={'X-CSRF-Token':csrf}
        invitation=await(await client.post('/api/telegram',json={'action':'invite'},headers=headers)).json()
        assert invitation['expires_in']==600
        r=controller.notifications.recipients
        r.process(controller.settings,update(1,1001,'/start '+invitation['code']),lambda:'status')
        users=await(await client.get('/api/telegram')).json()
        assert users['users'][0]['chat_id']=='1001'
        await client.post('/api/telegram',json={'action':'remove','chat_id':'1001'},headers=headers)
        assert not r.effective(controller.settings)
