const main = document.querySelector('#main');
let csrf = '';
let timer;

class RequestError extends Error {
  constructor(message, code) { super(message); this.code = code; }
}

async function api(path, body) {
  let response;
  try {
    response = await fetch(path, {
      method: body === undefined ? 'GET' : 'POST',
      credentials: 'same-origin', cache: 'no-store',
      headers: body === undefined ? {} : {'Content-Type': 'application/json', 'X-Inky-CSRF': csrf},
      body: body === undefined ? undefined : JSON.stringify(body)
    });
  } catch { throw new RequestError('Inky cannot be reached. Check your connection and try again.', 'offline'); }
  let result;
  try { result = await response.json(); }
  catch { throw new RequestError('This address did not return an Inky response. Check the address, then try again.', 'invalid_response'); }
  if (!response.ok || !result.ok) throw new RequestError(result.error?.message || 'Inky could not complete this request. Try again.', result.error?.code);
  return result.data;
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function button(text, action, className = 'secondary') {
  const node = element('button', text, className);
  node.type = 'button';
  node.addEventListener('click', action);
  return node;
}

function screen(title, description) {
  clearInterval(timer);
  document.title = `${title} | Inky`;
  const heading = element('h1', title);
  heading.tabIndex = -1;
  main.replaceChildren(heading, element('p', description, 'intro'));
  main.setAttribute('aria-busy', 'false');
  heading.focus();
}

function errorMessage(node, text) {
  node.textContent = text;
  node.hidden = false;
  node.focus();
}

function help(title, paragraphs, command) {
  const details = element('details');
  details.append(element('summary', title));
  for (const text of paragraphs) details.append(element('p', text));
  if (command) {
    details.append(element('p', 'On the computer running Inky, use your own configuration file:'));
    const pre = element('pre');
    pre.append(element('code', command));
    details.append(pre, element('p', 'Use a private terminal. Do not paste the code into a link, screenshot, or public message.'));
  }
  return details;
}

function codeHelp() {
  return help('Where do I get the setup code?', [
    'The code comes from the computer running Inky. It confirms that you can control this installation.',
    'If someone installed Inky for you, ask them for a fresh code. It may be in the secure note they gave you. It is not a password or a bird-service API key.',
    'If you installed Inky yourself, create a code with the command below. In a source checkout, start the command with uv run. For Docker, run it with docker compose exec controller and use /data/config.toml.'
  ], 'inky-bird-frame access bootstrap --config <config.toml> --show-code');
}

function passwordField(form, setup, access) {
  const label = element('label', setup ? 'Choose your Inky password' : 'Inky password');
  const input = element('input');
  input.id = 'password'; input.name = 'password'; input.type = 'password'; input.required = true;
  input.autocomplete = setup ? 'new-password' : 'current-password';
  const row = element('div', '', 'input-row');
  const reveal = button('Show', () => {
    const shown = input.type === 'text';
    input.type = shown ? 'password' : 'text';
    reveal.textContent = shown ? 'Show' : 'Hide';
    reveal.setAttribute('aria-label', shown ? 'Show password' : 'Hide password');
    reveal.setAttribute('aria-pressed', String(!shown));
  }, 'reveal');
  reveal.setAttribute('aria-label', 'Show password');
  reveal.setAttribute('aria-pressed', 'false');
  reveal.setAttribute('aria-controls', input.id);
  label.htmlFor = input.id;
  const hint = element('p', setup
    ? `Use at least ${access.password_min_length} characters. A few unrelated words work well. Save it in your password manager.`
    : 'Use the password you chose for this Inky. You do not need the setup code again.', 'hint');
  hint.id = 'password-hint';
  input.setAttribute('aria-describedby', hint.id);
  if (setup) input.addEventListener('input', () => {
    // Python counts Unicode code points, unlike HTML minlength's UTF-16 units.
    const length = Array.from(input.value).length;
    input.setCustomValidity(length < access.password_min_length || length > access.password_max_length
      ? `Use between ${access.password_min_length} and ${access.password_max_length} characters.` : '');
  });
  row.append(input, reveal);
  form.append(label, row, hint);
  return input;
}

function progress(step) {
  const steps = element('ol', '', 'steps');
  steps.setAttribute('aria-label', 'Setup progress');
  for (const [index, text] of ['Setup code', 'Your password'].entries()) {
    const item = element('li', `${index + 1}. ${text}`, index + 1 === step ? 'current' : '');
    if (index + 1 === step) item.setAttribute('aria-current', 'step');
    steps.append(item);
  }
  main.append(steps);
}

function setup(access, secret = '', deadline = performance.now() + (access.setup_code.expires_in_seconds || 0) * 1000) {
  const secondStep = Boolean(secret);
  screen(secondStep ? 'Make Inky yours' : 'Welcome to Inky', secondStep
    ? 'Choose a password to protect the controls for your bird journal. No email or online account is needed.'
    : 'Connect this browser to your Inky installation. You only need a setup code the first time.');
  main.append(element('p', '✓ Connection checked', 'connection'));
  progress(secondStep ? 2 : 1);
  const form = element('form');
  const status = element('p', '', 'error');
  status.hidden = true; status.tabIndex = -1; status.setAttribute('role', 'alert');
  const guidance = codeHelp();
  let input;
  if (secondStep) {
    form.append(element('p', 'Setup code entered', 'hint'));
    input = passwordField(form, true, access);
  } else {
    const label = element('label', 'Setup code'); label.htmlFor = 'setup-code';
    input = element('input'); input.id = label.htmlFor; input.name = 'setup-code';
    input.type = 'password'; input.autocomplete = 'off'; input.required = true; input.maxLength = 128;
    input.spellcheck = false; input.autocapitalize = 'none';
    input.addEventListener('input', () => input.setCustomValidity(''));
    input.setAttribute('aria-describedby', 'code-hint');
    const hint = element('p', 'Paste the code from your installer or the computer running Inky.', 'hint');
    hint.id = 'code-hint';
    form.append(label, input, hint);
  }
  const expiry = element('p', '', 'code-status');
  expiry.setAttribute('role', 'status');
  const submit = element('button', secondStep ? 'Finish setup' : 'Continue'); submit.type = 'submit';
  const check = button('Check for a new code', () => load());
  check.hidden = true;
  function showExpiry() {
    const remaining = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
    const ready = access.setup_code.state === 'ready' && remaining > 0;
    submit.disabled = !ready;
    input.disabled = !ready;
    check.hidden = ready;
    // Announce meaningful changes, not a new live-region message every second.
    const minutes = Math.ceil(remaining / 60);
    const text = ready ? `A code is ready. It expires in ${minutes} minute${minutes === 1 ? '' : 's'}.`
      : access.setup_code.state === 'missing' ? 'Create a setup code to continue. Open the help below for the steps.'
      : 'The setup code has expired. Get a fresh code, then check again. Your birds are unchanged.';
    if (expiry.textContent !== text) expiry.textContent = text;
    if (!ready) { guidance.open = true; clearInterval(timer); }
  }
  form.append(expiry, submit, check, status);
  const back = secondStep ? button('Change setup code', () => load()) : null;
  if (back) form.append(back);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!secondStep) {
      if (!input.value.trim()) {
        input.setCustomValidity('Paste the full setup code to continue.');
        input.reportValidity();
        return;
      }
      setup(access, input.value.trim(), deadline);
      return;
    }
    submit.disabled = true; submit.textContent = 'Setting up…';
    form.setAttribute('aria-busy', 'true');
    if (back) back.disabled = true;
    clearInterval(timer); status.hidden = true;
    try {
      const session = await api('/v1/owner/setup', {secret, passphrase: input.value});
      csrf = session.csrf; secret = ''; input.value = '';
      await signedIn();
    } catch (error) {
      if (error.code === 'sign_in_required') {
        secret = ''; input.value = '';
        const latest = await api('/v1/access').catch(() => null);
        if (latest?.secure) {
          if (!latest.setup_required) { signIn(latest, 'This Inky is already set up. Sign in with your password.'); return; }
          setup(latest);
          errorMessage(main.querySelector('.error'), latest.setup_code.state === 'ready'
            ? 'That code did not work. Copy the full, newest setup code and try again.'
            : 'Get a fresh setup code before you continue.');
          return;
        }
      }
      errorMessage(status, error.message);
      form.setAttribute('aria-busy', 'false');
      submit.textContent = 'Finish setup'; showExpiry();
      if (back) back.disabled = false;
      if (!submit.disabled) timer = setInterval(showExpiry, 1000);
    }
  });
  main.append(form, guidance, help('Why do I need a code and a password?', [
    'The one-use code proves you can access the computer running Inky. It prevents someone else on your network from claiming your installation.',
    'Your password is what you use after setup. It protects settings and private bird history. Inky stores a protected hash, not the password itself.'
  ]));
  showExpiry();
  if (!submit.disabled) timer = setInterval(showExpiry, 1000);
}

function signIn(access, notice = '') {
  screen('Welcome back', 'Sign in to your Inky bird journal.');
  const form = element('form');
  if (notice) form.append(element('p', notice, 'hint'));
  const input = passwordField(form, false, access);
  const submit = element('button', 'Sign in'); submit.type = 'submit';
  const status = element('p', '', 'error');
  status.hidden = true; status.tabIndex = -1; status.setAttribute('role', 'alert');
  form.append(submit, status);
  form.addEventListener('submit', async event => {
    event.preventDefault(); submit.disabled = true; submit.textContent = 'Signing in…'; status.hidden = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const session = await api('/v1/owner/login', {passphrase: input.value});
      csrf = session.csrf; input.value = ''; await signedIn();
    } catch (error) {
      errorMessage(status, error.code === 'sign_in_required'
        ? 'That password did not work. Check it and try again.' : error.message);
    } finally { submit.disabled = false; submit.textContent = 'Sign in'; form.setAttribute('aria-busy', 'false'); }
  });
  main.append(form, help('Forgot your password?', [
    'Inky has no email account, so it cannot send a reset link. You can reset access from the computer running Inky.',
    'Reset signs out every browser. Your saved bird data, plates, and bird-service accounts stay in place. If someone installed Inky for you, ask them to reset access.'
  ], 'inky-bird-frame access reset --config <config.toml> --yes --show-code'));
}

async function signedIn() {
  const session = await api('/v1/owner/session');
  csrf = session.csrf;
  screen('You’re connected to Inky', 'Setup is complete. This browser can now sign in to your installation.');
  main.append(element('p', '✓ Sign-in checked', 'connection'));
  const notice = element('section', '', 'notice');
  notice.append(element('h2', 'What this preview does'), element('p', 'You can test setup, reload, sign-out, and sign-in here. The full bird journal is still being built. This preview does not manage your birds yet.'));
  const out = button('Sign out', async () => {
    out.disabled = true;
    try { await api('/v1/owner/logout', {}); csrf = ''; await load('You signed out.'); }
    catch (error) {
      if (error.code === 'sign_in_required') { csrf = ''; await load('Your session ended. Sign in again.'); }
      else { out.disabled = false; showProblem(error); }
    }
  });
  main.append(notice, out);
}

function showProblem(error) {
  if (error.code === 'access_error') {
    screen('Inky needs a local repair', 'Inky cannot open its private sign-in settings. Ask the person who installed Inky to check its storage and file permissions.');
    main.append(help('Details for the installer', [error.message, 'Do not delete private state to clear this error. Check the installation guide and use a matching private backup if needed.']), button('Try again', () => load()));
    return;
  }
  screen('Inky needs a connection', error.message);
  main.append(button('Try again', () => load()), element('p', 'Keep your home network or private VPN connected. If the problem continues, check that the Inky service is running.', 'hint'));
}

async function load(notice = '') {
  clearInterval(timer);
  main.setAttribute('aria-busy', 'true');
  try {
    const access = await api('/v1/access');
    if (!access.secure) {
      screen('Use a private connection', 'This address is not approved for sign-in. Inky has turned password entry off to protect you.');
      main.append(element('p', 'Password entry is off', 'notice'), help('How do I open Inky safely?', [
        'On a phone or tablet, open the private HTTPS address provided when Inky was installed. HTTPS means the connection encrypts your password.',
        'On the computer running Inky, you can use its local address instead. Ask your installer for that address if you do not know it.',
        'When away from home, connect through your private VPN first. A VPN connects your device to your home network. Do not ignore a browser certificate warning.'
      ]), button('Check connection again', () => load()));
      main.querySelector('details').open = true;
      return;
    }
    try { await signedIn(); }
    catch (error) {
      if (error.code !== 'sign_in_required') throw error;
      csrf = '';
      if (access.setup_required) setup(access);
      else signIn(access, notice);
    }
  } catch (error) { showProblem(error); }
  finally { main.setAttribute('aria-busy', 'false'); }
}

await load();
