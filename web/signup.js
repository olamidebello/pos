'use strict';
document.getElementById('signupForm').addEventListener('submit',async event=>{
  event.preventDefault();const form=event.currentTarget,status=document.getElementById('signupStatus'),button=form.querySelector('button');
  const data=Object.fromEntries(new FormData(form));
  if(data.password!==data.confirm){status.textContent='Passwords must match.';return;}
  delete data.confirm;data.accept_terms=form.elements.accept_terms.checked;button.disabled=true;status.textContent='Creating account…';
  try{const response=await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),cache:'no-store'});const result=await response.json();if(!response.ok)throw Error(result.error||'Unable to create account');form.reset();status.textContent=result.message;form.hidden=true;}
  catch(error){status.textContent=error.message;}finally{button.disabled=false;}
});
