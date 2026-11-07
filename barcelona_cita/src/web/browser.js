import RFB from './novnc/core/rfb.js';
let rfb, disconnected=false;
const connection=document.getElementById('connection');
async function connect() {
  if(disconnected)return;
  const response=await fetch('api/status',{cache:'no-store'});const status=await response.json();
  if(disconnected)return;
  if(status.busy){document.getElementById('warning').textContent='Автоматическая проверка продолжается. Подключение управления произойдёт после её завершения.';connection.textContent='Идёт автоматическая проверка. Ожидаем результат; управление ещё не передано.';setTimeout(()=>connect().catch(()=>connection.textContent='Ошибка подключения'),3000);return;}
  if(!status.desktop_available){connection.textContent='Пригодной сессии нет. Вернитесь в панель и используйте «Восстановить работу».';return;}
  document.getElementById('warning').textContent='Подключение к текущей вкладке приостановит автоматизацию. Сохранение сессии не резервирует место.';
  const url=new URL('ws',location.href);url.protocol=location.protocol==='https:'?'wss:':'ws:';
  rfb=new RFB(document.getElementById('screen'),url.href,{wsProtocols:['binary','cita-csrf.'+status.csrf]});
  rfb.scaleViewport=true;rfb.resizeSession=false;rfb.showDotCursor=true;
  rfb.addEventListener('connect',()=>connection.textContent='Текущая сессия · ручное управление');
  rfb.addEventListener('disconnect',()=>connection.textContent='Отключено. Управление освобождено. Вернитесь в панель для завершения сессии и возобновления мониторинга.');
}
document.getElementById('disconnect').onclick=()=>{disconnected=true;rfb?.disconnect();connection.textContent='Подключение отключено.';};
const keyboard=document.getElementById('keyboard-input');
document.getElementById('keyboard').onclick=()=>keyboard.focus();
keyboard.addEventListener('input',()=>{for(const c of keyboard.value)rfb?.sendKey(c.codePointAt(0));keyboard.value='';});
keyboard.addEventListener('keydown',e=>{if(e.key==='Backspace'){rfb?.sendKey(0xff08);e.preventDefault();}if(e.key==='Enter'){rfb?.sendKey(0xff0d);e.preventDefault();}});
setInterval(async()=>{try{const s=await(await fetch('api/status',{cache:'no-store'})).json();if(s.state==='SESSION_EXPIRED'||s.state==='BLOCKED')document.getElementById('warning').textContent='Сессия неактуальна или доступ ограничен. Наличие свободного места больше не подтверждается. '+s.last_response;}catch{}},5000);
connect().catch(()=>connection.textContent='Не удалось подключиться. Вернитесь в панель управления.');
