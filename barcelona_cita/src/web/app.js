let csrf = '', profileData = [], settingsData = {};
const $ = s => document.querySelector(s);
const states = {IDLE:'Ожидание',NAVIGATING:'Переход по страницам',CHECKING:'Проверка ответа',NO_SLOTS:'Записей нет',POSSIBLE_SLOTS:'Возможны записи',AWAITING_USER:'Требуется человек',BOOKING_IN_PROGRESS:'Ручное оформление',BOOKED:'Подтверждение обнаружено',SESSION_EXPIRED:'Сессия неактуальна',BLOCKED:'Доступ ограничен',ERROR:'Техническая ошибка'};
const responses = {NO_SLOTS:'En este momento no hay citas disponibles',POSSIBLE_SLOTS_UNRESERVED:'Следующий шаг доступен; место не зарезервировано',UNKNOWN_PAGE:'Неизвестный экран — требуется человек',HUMAN_VERIFICATION_REQUIRED:'CAPTCHA / SMS — требуется человек',PORTAL_BLOCKED:'Портал ограничил доступ'};
const time = v => v ? new Date(v).toLocaleString('ru-RU', {timeZone:'Europe/Madrid'}) : '—';
function feedback(text, error=false) { $('#feedback').textContent=text; $('#feedback').className=error?'error':'success'; }
async function api(path, data) {
  const response=await fetch(path, {method:data===undefined?'GET':'POST',cache:'no-store',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:data===undefined?undefined:JSON.stringify(data)});
  const result=await response.json(); if(!response.ok) throw new Error(result.error || 'Ошибка запроса'); return result;
}
function item(tag,text) { const el=document.createElement(tag);el.textContent=text;return el; }
async function refresh() {
  try {
    const s=await api('api/status');csrf=s.csrf;
    $('#state').textContent=states[s.state] || s.state;
    $('#status').replaceChildren();
    for (const [label,value] of Object.entries({'Заявитель':s.active_profile||'—','Последняя проверка':time(s.last_check),'Следующая проверка':time(s.next_check),'Проверок сегодня':s.checks_today,'Ответ портала':responses[s.last_response]||s.last_response,'Браузер':s.browser_alive?'Сессия открыта':'Закрыт','Управление':s.human_control?'У пользователя':'Автоматическое','Расписание':s.paused?'Пауза':s.monitoring?'Запущено':'Остановлено','Возможная запись':s.possible_slots?'Требует проверки; не зарезервирована':'Не обнаружена / неактуальна','Память контейнера':s.memory_mb+' МБ'})) {
      const el=item('div','');el.append(item('span',label),item('strong',String(value)));$('#status').append(el);
    }
    $('#bookings').replaceChildren(...s.bookings.map(b=>item('p',`${b.profile===1?'Антон':'Наталья'} · ${b.appointment} · ${b.office}`)));
    $('#events').replaceChildren(...s.events.slice(0,40).map(e=>item('p',`${time(e.at)} · ${e.profile===1?'Антон':e.profile===2?'Наталья':'Система'} · ${e.code}`)));
  } catch(e) { feedback(e.message,true); }
}
async function command(action, extra={}) { await api('api/command',{action,...extra}); await refresh();feedback('Команда принята.'); }
document.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>command(b.dataset.action).catch(e=>feedback(e.message,true)));
$('#switch').onclick=()=>command('select',{profile_id:Number($('#select-profile').value)}).catch(e=>feedback(e.message,true));
$('#discard').onclick=()=>{if(window.confirm('Закрыть текущую вкладку и потерять возможность продолжения?'))command('discard').catch(e=>feedback(e.message,true));};
$('#confirmation').onsubmit=async e=>{e.preventDefault();try{await command('verify',Object.fromEntries(new FormData(e.target)));e.target.reset();}catch(err){feedback(err.message,true);}};
function field(parent,key,label,value,type='text') {
  const l=item('label',label), input=document.createElement('input'); input.name=key;input.type=type;
  if(type==='checkbox') input.checked=Boolean(value); else input.value=value??'';
  if(type==='password')input.autocomplete='new-password';
  if(type==='number' && key.includes('minutes') && !key.startsWith('session_'))input.min='15';
  l.append(input);parent.append(l); return input;
}
async function forms() {
  profileData=await api('api/profiles');
  for(const p of profileData) {
    const section=document.createElement('fieldset');section.dataset.id=p.id;section.append(item('legend',p.label));
    field(section,'enabled','Включён',p.enabled,'checkbox');
    for(const [k,l] of [['full_name','Имя и фамилия (как в документах)'],['nie','NIE'],['nationality','Национальность'],['email','Email'],['phone','Телефон']])field(section,k,l,p[k],k==='email'?'email':'text');
    section.append(item('p',p.booked?'Запись подтверждена; проверки исключены':`Статус: ${states[p.status]}`));$('#profile-fields').append(section);
  }
  settingsData=await api('api/settings');
  const labels={automatic:'Автоматическое расписание',interval_minutes:'Обычный интервал, минут',thursday_enabled:'Приоритетный четверг',priority_minutes:'Интервал четверга, минут',morning_enabled:'Утреннее окно',morning_minutes:'Утренний интервал, минут',session_warning_minutes:'Предупреждение об ожидании, минут',session_expiry_minutes:'Предел актуальности сессии, минут',memory_limit_mb:'Бюджет памяти контейнера, МБ',date_from:'Допустимые даты с',date_to:'Допустимые даты по',offices:'Допустимые отделения (через запятую)',ingress_url:'HTTPS ссылка HA: /hassio/ingress/<slug>',ha_notifications:'Уведомления Home Assistant',ha_sensors:'Диагностические состояния HA',telegram_enabled:'Telegram',telegram_token:'Telegram bot token (пустое поле сохраняет текущий)',telegram_chat_ids:'Разрешённые chat ID (через запятую)',ha_telegram_action:'Вместо token: действие HA notify.<имя>',smtp_enabled:'Резервное уведомление email',smtp_host:'SMTP сервер',smtp_port:'SMTP порт',smtp_starttls:'STARTTLS (иначе SSL/TLS)',smtp_username:'SMTP пользователь',smtp_password:'SMTP пароль (пустое поле сохраняет текущий)',smtp_sender:'Email отправителя',smtp_recipients:'Разрешённые email (через запятую)',debug:'Диагностика (только коды, без HTML и снимков)'};
  for(const [key,label] of Object.entries(labels)) {
    const value=settingsData[key];let type=typeof value==='boolean'?'checkbox':typeof value==='number'?'number':'text';
    if(['telegram_token','smtp_password'].includes(key))type='password';if(key.startsWith('date_'))type='date';
    const input=field($('#settings-fields'),key,label,Array.isArray(value)?value.join(', '):value,type);
    if(settingsData.secret_fields_set.includes(key))input.placeholder='Сохранено';
  }
  for(const key of ['telegram_token','smtp_password'])field($('#settings-fields'),'clear_'+key,'Удалить сохранённый '+(key==='telegram_token'?'Telegram token':'SMTP пароль'),false,'checkbox');
}
$('#profiles').onsubmit=async e=>{e.preventDefault();try{
  const raw=profileData.map(p=>{const s=document.querySelector(`fieldset[data-id="${p.id}"]`);const q={...p};s.querySelectorAll('input').forEach(i=>q[i.name]=i.type==='checkbox'?i.checked:i.value);return q;});
  await api('api/profiles',raw);profileData=raw;feedback('Профили сохранены.');await refresh();
}catch(err){feedback(err.message,true);}};
$('#settings').onsubmit=async e=>{e.preventDefault();try{
  const raw={...settingsData}, clear=[];delete raw.secret_fields_set;
  e.target.querySelectorAll('input').forEach(i=>{if(i.name.startsWith('clear_')){if(i.checked)clear.push(i.name.slice(6));return;}raw[i.name]=i.type==='checkbox'?i.checked:i.type==='number'?Number(i.value):Array.isArray(settingsData[i.name])?i.value.split(',').map(x=>x.trim()).filter(Boolean):i.type==='date'?(i.value||null):i.value;});
  await api('api/settings',{...raw,clear_secrets:clear});settingsData=await api('api/settings');e.target.querySelectorAll('input[type=password]').forEach(i=>i.value='');feedback('Настройки сохранены.');await refresh();
}catch(err){feedback(err.message,true);}};
(async()=>{await refresh();await forms();setInterval(refresh,5000);})().catch(e=>feedback(e.message,true));
