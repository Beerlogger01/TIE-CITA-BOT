import RFB from './novnc/core/rfb.js';
let rfb;
const connection=document.getElementById('connection');
async function connect() {
  const response=await fetch('api/status',{cache:'no-store'});const status=await response.json();
  if(!status.browser_alive){connection.textContent='Активной сессии нет. Вернитесь в панель.';return;}
  const url=new URL('ws',location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
  rfb=new RFB(document.getElementById('screen'),url.href,{wsProtocols:['binary','cita-csrf.'+status.csrf]});
  rfb.scaleViewport=true;rfb.resizeSession=false;rfb.showDotCursor=true;
  rfb.addEventListener('connect',()=>connection.textContent='Текущая сессия · ручное управление');
  rfb.addEventListener('disconnect',()=>connection.textContent='Отключено. Автоматизация остаётся на паузе.');
}
document.getElementById('disconnect').onclick=()=>rfb?.disconnect();
const keyboard=document.getElementById('keyboard-input');
document.getElementById('keyboard').onclick=()=>keyboard.focus();
keyboard.addEventListener('input',()=>{for(const c of keyboard.value)rfb?.sendKey(c.codePointAt(0));keyboard.value='';});
keyboard.addEventListener('keydown',e=>{if(e.key==='Backspace'){rfb?.sendKey(0xff08);e.preventDefault();}if(e.key==='Enter'){rfb?.sendKey(0xff0d);e.preventDefault();}});
setInterval(async()=>{try{const s=await(await fetch('api/status',{cache:'no-store'})).json();if(s.state==='SESSION_EXPIRED'||s.state==='BLOCKED')document.getElementById('warning').textContent='Сессия неактуальна или доступ ограничен. Наличие свободного места больше не подтверждается. '+s.last_response;}catch{}},5000);
connect().catch(()=>connection.textContent='Не удалось подключиться. Вернитесь в панель управления.');
