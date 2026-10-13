const main = document.querySelector('#main');
let csrf = '';

class RequestError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}

async function api(path, body) {
  const response = await fetch(path, {
    method: body === undefined ? 'GET' : 'POST',
    credentials: 'same-origin', cache: 'no-store',
    headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Inky-CSRF': csrf},
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  let result;
  try { result = await response.json(); }
  catch { throw new RequestError('Inky returned an invalid response. Check the service and connection.', 'invalid_response'); }
  if (!response.ok || !result.ok) throw new RequestError(result.error?.message || 'Inky could not complete this request.', result.error?.code);
  return result.data;
}

function message(text) {
  const p = document.createElement('p');
  p.setAttribute('role', 'status');
  p.textContent = text;
  return p;
}

function signIn(setup) {
  main.replaceChildren();
  const heading = document.createElement('h1');
  heading.textContent = setup ? 'Set up owner access' : 'Open your field journal';
  const form = document.createElement('form');
  const status = message('');
  const fields = {};
  for (const [key, labelText] of setup ? [['secret', 'One-use setup secret'], ['passphrase', 'Choose a passphrase (at least 15 characters)']] : [['passphrase', 'Owner passphrase']]) {
    const label = document.createElement('label');
    label.textContent = labelText;
    const input = document.createElement('input');
    input.name = key; input.type = 'password'; input.required = true;
    input.autocomplete = key === 'secret' ? 'off' : setup ? 'new-password' : 'current-password';
    input.maxLength = key === 'secret' ? 128 : 1024;
    if (setup && key === 'passphrase') input.minLength = 15;
    label.append(input); form.append(label); fields[key] = input;
  }
  const button = document.createElement('button');
  button.type = 'submit'; button.textContent = setup ? 'Establish owner access' : 'Sign in';
  form.append(button, status);
  form.addEventListener('submit', async event => {
    event.preventDefault(); button.disabled = true; status.textContent = '';
    const body = Object.fromEntries(Object.entries(fields).map(([key, input]) => [key, input.value]));
    try {
      const session = await api(setup ? '/v1/owner/setup' : '/v1/owner/login', body);
      csrf = session.csrf;
      for (const input of Object.values(fields)) input.value = '';
      await signedIn();
    } catch (error) { status.textContent = error.message; }
    finally { for (const key of Object.keys(body)) body[key] = ''; button.disabled = false; }
  });
  main.append(heading, form);
  if (setup) main.append(message('Create this secret with the local access bootstrap command. It expires after 10 minutes. Never put it in a link.'));
  fields[setup ? 'secret' : 'passphrase'].focus();
}

async function signedIn() {
  const session = await api('/v1/owner/session');
  csrf = session.csrf;
  const heading = document.createElement('h1'); heading.textContent = 'Owner access is ready';
  const button = document.createElement('button'); button.textContent = 'Sign out';
  button.addEventListener('click', async () => {
    try { await api('/v1/owner/logout', {}); csrf = ''; signIn(false); }
    catch (error) {
      if (error.code === 'sign_in_required') { csrf = ''; signIn(false); }
      main.append(message(error.message));
    }
  });
  main.replaceChildren(heading, message('This isolated build is proving the secure access boundary. The full field-journal journeys are not implemented yet.'), button);
}

try {
  const access = await api('/v1/access');
  if (!access.secure) {
    main.replaceChildren(message('Use HTTPS or open Inky on the controller’s genuine loopback address. This connection does not accept credentials.'));
  } else {
    try { await signedIn(); }
    catch (error) {
      if (error.code === 'sign_in_required') signIn(access.setup_required);
      else throw error;
    }
  }
} catch (error) { main.replaceChildren(message(error.message)); }
finally { main.setAttribute('aria-busy', 'false'); }
