'use strict';
let installPrompt=null;
const installButton=document.getElementById('installNow');
const installStatus=document.getElementById('installStatus');
function setInstallStatus(text){if(installStatus)installStatus.textContent=text;}
if('serviceWorker' in navigator&&window.isSecureContext){
  navigator.serviceWorker.register('/sw.js').catch(()=>setInstallStatus('Automatic setup is unavailable. You can still browse Global POS in this browser.'));
}
const installed=()=>window.matchMedia('(display-mode: standalone)').matches||navigator.standalone===true;
if(installed()){setInstallStatus('Global POS is already running as an installed app.');}
window.addEventListener('beforeinstallprompt',event=>{
  event.preventDefault();installPrompt=event;
  if(installButton&&!installed())installButton.hidden=false;
  setInstallStatus('Global POS is ready to install on this device.');
});
window.addEventListener('appinstalled',()=>{installPrompt=null;if(installButton)installButton.hidden=true;setInstallStatus('Global POS installed. Open it from your home screen.');});
if(installButton)installButton.addEventListener('click',async()=>{
  if(!installPrompt)return;
  const prompt=installPrompt;installPrompt=null;installButton.hidden=true;
  try{await prompt.prompt();const result=await prompt.userChoice;setInstallStatus(result.outcome==='accepted'?'Installation accepted. Check your home screen.':'Installation dismissed. You can use the browser menu to install later.');}
  catch{setInstallStatus('Use your browser menu to install, or continue in the browser.');}
});
