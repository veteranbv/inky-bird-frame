import { openJournal, closeJournal, retainSignedOutFrame } from '/app/journal.js';

const main = document.querySelector('#main');
let csrf = '';
let lastActivity = -Infinity;
let signingOut = false;
let accountBackAvailable = false;

// Only real interaction renews idle time. Polls and synthetic events do not.
for (const name of ['pointerdown', 'keydown'])
  document.addEventListener(name, (event) => {
    if (!csrf || !event.isTrusted || performance.now() - lastActivity < 60000) return;
    lastActivity = performance.now();
    const sent = csrf;
    api('/v1/owner/activity', {}).catch((error) => {
      if (error.code === 'sign_in_required' && csrf === sent && !signingOut) showProblem(error);
    });
  });

class RequestError extends Error {
  constructor(message, code) {
    super(message);
    this.code = code;
  }
}

async function api(path, body) {
  return request(path, {
    method: body === undefined ? 'GET' : 'POST',
    headers: body === undefined ? {} : { 'Content-Type': 'application/json', 'X-Inky-CSRF': csrf },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

async function upload(path, file, signal) {
  return request(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/octet-stream', 'X-Inky-CSRF': csrf },
    body: file,
    signal,
  });
}

async function request(path, options) {
  let response;
  try {
    response = await fetch(path, {
      ...options,
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new RequestError(
      'Inky cannot be reached. Check your connection and try again.',
      'offline',
    );
  }
  let result;
  try {
    result = await response.json();
  } catch {
    throw new RequestError(
      response.ok
        ? 'This address did not return an Inky response. Check the address and try again.'
        : 'Inky is not responding right now (HTTP ' +
            response.status +
            '). Check that it is running.',
      'invalid_response',
    );
  }
  if (!response.ok || !result.ok)
    throw new RequestError(
      result.error?.message || 'Inky could not complete this request.',
      result.error?.code,
    );
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
  closeJournal();
  document.title = title + ' | Inky';
  const heading = element('h1', title);
  heading.tabIndex = -1;
  main.replaceChildren(heading, element('p', description, 'intro'));
  main.setAttribute('aria-busy', 'false');
  heading.focus();
}

function statusBox() {
  const node = element('p', '', 'error');
  node.setAttribute('role', 'alert');
  return node;
}

function announce(node, text) {
  node.textContent = '';
  requestAnimationFrame(() => {
    if (node.isConnected) node.textContent = text;
  });
}

function errorMessage(node, text) {
  announce(node, text);
}

function noticeMessage(text) {
  const node = element('p', text, 'notice');
  node.setAttribute('role', 'status');
  return node;
}

function help(title, paragraphs, command) {
  const details = element('details');
  details.append(element('summary', title));
  for (const text of paragraphs) details.append(element('p', text));
  if (command) {
    const pre = element('pre');
    pre.append(element('code', command));
    details.append(pre);
  }
  return details;
}

function usernameField(form, value = '') {
  const label = element('label', 'Username');
  label.htmlFor = 'username';
  const input = element('input');
  input.id = label.htmlFor;
  input.name = 'username';
  input.autocomplete = 'username';
  input.autocapitalize = 'none';
  input.spellcheck = false;
  input.required = true;
  input.minLength = 3;
  input.maxLength = 64;
  input.value = value;
  input.pattern = '[a-zA-Z0-9][a-zA-Z0-9_.\\-]{2,63}';
  const hint = element(
    'p',
    'Use 3–64 letters, numbers, dots, hyphens or underscores. Start with a letter or number. Letter case does not matter.',
    'hint',
  );
  hint.id = 'username-hint';
  input.setAttribute('aria-describedby', hint.id);
  input.addEventListener('input', () => {
    input.setCustomValidity('');
    if (input.validity.patternMismatch) input.setCustomValidity(hint.textContent);
  });
  form.append(label, input, hint);
  return input;
}

function passwordField(form, setup, access, id = 'password', title, optional = false) {
  const label = element('label', title || (setup ? 'Choose your Inky password' : 'Inky password'));
  const input = element('input');
  input.id = id;
  input.name = id;
  input.type = 'password';
  input.required = !optional;
  input.autocomplete = setup ? 'new-password' : 'current-password';
  label.htmlFor = id;
  const row = element('div', '', 'input-row');
  const reveal = button(
    'Show',
    () => {
      const shown = input.type === 'text';
      input.type = shown ? 'password' : 'text';
      reveal.textContent = shown ? 'Show' : 'Hide';
      reveal.setAttribute('aria-label', shown ? 'Show password' : 'Hide password');
    },
    'reveal',
  );
  reveal.setAttribute('aria-label', 'Show password');
  reveal.setAttribute('aria-controls', id);
  row.append(input, reveal);
  form.append(label, row);
  if (setup) {
    if (id !== 'confirm-password') {
      const hint = element(
        'p',
        'Use at least ' +
          access.password_min_length +
          ' characters. A few unrelated words work well.',
        'hint',
      );
      hint.id = id + '-hint';
      input.setAttribute('aria-describedby', hint.id);
      form.append(hint);
    }
    input.addEventListener('input', () => {
      const length = Array.from(input.value).length;
      input.setCustomValidity(
        (length || !optional) &&
          (length < access.password_min_length || length > access.password_max_length)
          ? 'Use between ' +
              access.password_min_length +
              ' and ' +
              access.password_max_length +
              ' characters.'
          : '',
      );
    });
  }
  return input;
}

function rememberField(form, access) {
  const label = element('label', '', 'check-label');
  const input = element('input');
  input.type = 'checkbox';
  input.name = 'remember';
  label.append(input, document.createTextNode('Stay signed in on this device'));
  const hint = element(
    'p',
    'Only use this on a private device. Sign-in lasts at most ' +
      access.session_policy.remembered_days +
      ' days. Otherwise, it lasts up to ' +
      access.session_policy.regular_hours +
      ' hours. Your browser may end it sooner.',
    'hint',
  );
  hint.id = 'remember-hint';
  input.setAttribute('aria-describedby', hint.id);
  form.append(label, hint);
  return input;
}

function connectionNotice(access) {
  const hostTrust = element(
    'p',
    'Use this address only with web apps you trust. A different port does not keep sign-in cookies separate.',
    'hint',
  );
  if (access.encrypted) {
    const notice = element('p', '', 'connection');
    const check = element('span', '✓ ');
    check.setAttribute('aria-hidden', 'true');
    notice.append(check, document.createTextNode('Encrypted connection'));
    main.append(notice, hostTrust);
    return;
  }
  const section = element('section', '', 'notice');
  section.append(
    element(
      'p',
      'This connection is not encrypted. Use a unique password on a trusted home network. Do not expose Inky to the internet.',
    ),
  );
  section.append(hostTrust);
  section.append(
    help('About this home-network connection', [
      'People who can inspect your network traffic could read passwords, session credentials and private data. HTTPS encrypts that traffic. You can add it later, but Inky does not require it for trusted home-network use.',
      'Other web apps on this same hostname or IP address can receive your Inky sign-in cookie, even on a different port. If you do not trust those apps, use a separate device or an isolated address. A new HTTP hostname alone does not isolate them. See the access guide for HTTPS isolation.',
    ]),
  );
  main.append(section);
}

function signIn(access, notice = '') {
  const setup = access.setup_required;
  screen(
    setup ? 'Make Inky yours' : 'Welcome back',
    setup
      ? 'Create the owner account for your bird journal. No email or online account is needed.'
      : 'Sign in to your Inky bird journal.',
  );
  if (notice) main.append(noticeMessage(notice));
  connectionNotice(access);
  if (setup)
    main.append(
      element(
        'p',
        'This account is the owner of this Inky. Finish setup before you share its address.',
        'hint',
      ),
    );
  const form = element('form');
  const status = statusBox();
  const username = !setup && access.legacy_account ? null : usernameField(form);
  const password = passwordField(form, setup, access);
  const confirmation = setup
    ? passwordField(form, true, access, 'confirm-password', 'Confirm your password')
    : null;
  if (confirmation)
    confirmation.addEventListener('input', () => confirmation.removeAttribute('aria-invalid'));
  const remember = rememberField(form, access);
  const submit = element('button', setup ? 'Create account' : 'Sign in');
  submit.type = 'submit';
  form.append(submit, status);
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    status.textContent = '';
    if (confirmation && password.value !== confirmation.value) {
      errorMessage(status, 'The passwords do not match. Check both entries.');
      confirmation.setAttribute('aria-invalid', 'true');
      confirmation.focus();
      return;
    }
    submit.disabled = true;
    form.setAttribute('aria-busy', 'true');
    try {
      const body = { passphrase: password.value, remember: remember.checked };
      if (username) body.username = username.value;
      const result = await api(setup ? '/v1/owner/register' : '/v1/owner/login', body);
      csrf = result.csrf;
      password.value = '';
      if (confirmation) confirmation.value = '';
      await signedIn(access, setup ? 'Your owner account is ready.' : '');
    } catch (error) {
      if (setup && error.code === 'sign_in_required') {
        await load('This Inky already has an owner. Sign in instead.');
        return;
      }
      errorMessage(status, error.message);
    } finally {
      submit.disabled = false;
      form.setAttribute('aria-busy', 'false');
    }
  });
  if (setup || access.local_login) main.append(form);
  else if (access.provider?.linked && access.provider?.ready) {
    const preferences = element('div');
    rememberField(preferences, access);
    preferences.querySelector('input').replaceWith(remember);
    main.append(preferences, element('p', 'Use your linked single sign-on account.', 'hint'));
  } else
    main.append(
      element(
        'p',
        'Browser sign-in is unavailable. Ask the installer to restore the provider or enable local sign-in. Your data stays in place.',
        'notice',
      ),
    );
  if (!setup && access.provider?.linked && access.provider?.ready) {
    const sso = button('Sign in with ' + access.provider.label, async () => {
      sso.disabled = true;
      try {
        await beginSSO('/v1/owner/oidc/start', remember.checked);
      } catch (error) {
        sso.disabled = false;
        if (!main.contains(status)) main.append(status);
        errorMessage(status, error.message);
      }
    });
    main.append(sso);
  }
  if (access.provider?.error) main.append(element('p', access.provider.error, 'error'));
  if (!setup)
    main.append(
      help('Can’t sign in?', [
        'Recovery uses a private terminal on the computer running Inky. It replaces your username and password, signs out every device and unlinks single sign-on. It does not change bird data or bird-service accounts.',
        'The access guide below gives native and Docker commands. Use your installation’s configuration file. You will confirm the change and enter the new password in a hidden prompt. If someone installed Inky for you, ask them to recover access.',
      ]),
    );
  const docs = element('a', 'Download the access and recovery guide (.md)');
  docs.href = '/app/access-guide';
  docs.download = 'Inky-access-guide.md';
  main.append(docs);
}

async function beginSSO(path, remember = false, returnToAccess = false) {
  const result = await api(path, { remember, return_to_access: returnToAccess });
  location.assign(result.authorization_url);
}

async function signOut() {
  if (signingOut) return;
  signingOut = true;
  try {
    await api('/v1/owner/logout', {});
    csrf = '';
    accountBackAvailable = false;
    history.replaceState(null, '', '/app');
    await load('You signed out.');
  } catch (error) {
    showProblem(error);
  } finally {
    signingOut = false;
  }
}

async function signedIn(access, message = '') {
  const session = await api('/v1/owner/session');
  csrf = session.csrf;
  if (location.hash === '#account') {
    await accountAccess(access, session, message);
    return;
  }
  await openJournal(
    {
      api,
      upload,
      element,
      button,
      screen,
      signOut,
      showProblem,
      localPasswordAllowed: access.local_login,
      singleSignOnReady: session.single_sign_on && access.provider?.ready,
      confirmWithSSO: () => beginSSO('/v1/owner/oidc/start', session.remembered, true),
      sessionExpired: () => {
        csrf = '';
      },
      account: () => accountAccess(access, session),
    },
    message,
  );
}

async function accountAccess(access, session, message = '') {
  try {
    const [settings, devices] = await Promise.all([
      api('/v1/owner/access'),
      api('/v1/owner/sessions'),
    ]);
    if (location.hash !== '#account') {
      history.pushState(null, '', '/app#account');
      accountBackAvailable = true;
    }
    screen(
      'Account and access',
      'Keep sign-in simple. Add advanced options only when you need them.',
    );
    if (message) main.append(noticeMessage(message));
    let confirmationBox = null,
      busy = false;
    const actionErrors = new WeakMap();
    async function refresh(message) {
      session = await api('/v1/owner/session');
      csrf = session.csrf;
      await accountAccess(await api('/v1/access'), session, message);
    }
    function actionError(anchor, message) {
      let status = actionErrors.get(anchor);
      if (!status) {
        status = statusBox();
        actionErrors.set(anchor, status);
        anchor.after(status);
      }
      errorMessage(status, message);
    }
    function clearConfirmation() {
      confirmationBox?.remove();
      confirmationBox = null;
    }
    async function sensitive(action, anchor, usePassword = false, confirmationRequired = false) {
      if (busy) return;
      if (confirmationBox) {
        confirmationBox.querySelector('input,button')?.focus();
        return;
      }
      // The server decides freshness. Browser clock skew must not trap a user.
      if (!confirmationRequired) {
        busy = true;
        try {
          await action();
        } catch (error) {
          if (error.code === 'confirmation_required') {
            busy = false;
            await sensitive(action, anchor, false, true);
          } else if (error.code === 'sign_in_required') showProblem(error);
          else actionError(anchor, error.message);
        } finally {
          busy = false;
        }
        return;
      }
      confirmationBox = element('section', '', 'notice');
      anchor.after(confirmationBox);
      const status = statusBox();
      const cancel = button('Cancel confirmation', () => {
        clearConfirmation();
        (anchor.matches('button') ? anchor : anchor.querySelector('button[type=submit]'))?.focus();
      });
      if (session.single_sign_on && !usePassword && settings.provider?.ready) {
        const confirm = button('Confirm with single sign-on', async () => {
          confirm.disabled = true;
          try {
            await beginSSO('/v1/owner/oidc/start', session.remembered, true);
          } catch (error) {
            confirm.disabled = false;
            if (error.code === 'sign_in_required') showProblem(error);
            else errorMessage(status, error.message);
          }
        });
        confirmationBox.append(
          element(
            'p',
            'Sign in again to confirm this change. You will come back to Account and access. Unsaved entries will clear; then repeat the change.',
          ),
          confirm,
          status,
        );
        if (access.local_login)
          confirmationBox.append(
            button('Use my Inky password instead', () => {
              clearConfirmation();
              sensitive(action, anchor, true, true);
            }),
          );
        confirmationBox.append(cancel);
        confirmationBox.querySelector('button').focus();
        return;
      }
      if (!access.local_login) {
        confirmationBox.append(
          element(
            'p',
            'Your sign-in provider is unavailable. This change needs a fresh sign-in. Ask the installer to restore the provider, or enable local login through operator recovery.',
          ),
          cancel,
        );
        cancel.focus();
        return;
      }
      const confirmation = element('form');
      confirmation.append(element('h2', 'Confirm it’s you'));
      const current = passwordField(confirmation, false, access, 'current-password');
      const submit = element('button', 'Confirm and continue');
      submit.type = 'submit';
      confirmation.append(submit);
      confirmation.append(status, cancel);
      confirmationBox.append(confirmation);
      confirmation.addEventListener('submit', async (event) => {
        event.preventDefault();
        submit.disabled = true;
        try {
          await api('/v1/owner/confirm-password', { passphrase: current.value });
          current.value = '';
          session = await api('/v1/owner/session');
          csrf = session.csrf;
          clearConfirmation();
          await sensitive(action, anchor);
        } catch (error) {
          if (error.code === 'sign_in_required') showProblem(error);
          else errorMessage(status, error.message);
        } finally {
          submit.disabled = false;
        }
      });
      current.focus();
    }
    const account = help('Change your username or password', [
      'Leave the password fields empty to change only your username. This signs out every other device. Bird data stays unchanged.',
    ]);
    const form = element('form');
    const username = usernameField(form, settings.username || '');
    const password = passwordField(
      form,
      true,
      access,
      'password',
      'New Inky password (optional)',
      true,
    );
    const confirmation = passwordField(
      form,
      true,
      access,
      'confirm-password',
      'Confirm new password (optional)',
      true,
    );
    confirmation.addEventListener('input', () => confirmation.removeAttribute('aria-invalid'));
    const save = element('button', 'Save account');
    save.type = 'submit';
    const formStatus = statusBox();
    const savedNotice = element('p', '', 'connection');
    savedNotice.setAttribute('role', 'status');
    form.append(save, formStatus, savedNotice);
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      formStatus.textContent = '';
      savedNotice.textContent = '';
      if (
        !password.value &&
        !confirmation.value &&
        username.value.trim().toLowerCase() === settings.username
      ) {
        announce(savedNotice, 'No account changes to save.');
        return;
      }
      if (password.value !== confirmation.value) {
        errorMessage(formStatus, 'The passwords do not match.');
        confirmation.setAttribute('aria-invalid', 'true');
        confirmation.focus();
        return;
      }
      await sensitive(async () => {
        const body = { username: username.value };
        if (password.value) body.passphrase = password.value;
        const result = await api('/v1/owner/account', body);
        csrf = result.csrf;
        password.value = '';
        confirmation.value = '';
        await refresh('Account saved. Other devices were signed out.');
      }, form);
    });
    account.append(form);
    main.append(account);
    const sessions = element('section');
    sessions.append(element('h2', 'Signed-in devices'));
    sessions.append(
      element('p', 'Revoke a login you no longer use. Inky stores no browser fingerprint.', 'hint'),
    );
    for (const device of devices.sessions) {
      const row = element('div', '', 'device');
      const date = element(
        'p',
        (device.current ? 'This device · signed in ' : 'Signed in ') +
          new Date(device.created_at * 1000).toLocaleString(),
      );
      date.id = 'device-' + device.id;
      const revoke = button(
        device.current ? 'Sign out this device' : 'Revoke this sign-in',
        async () => {
          if (device.current) {
            await signOut();
          } else
            await sensitive(async () => {
              await api('/v1/owner/sessions/revoke', { id: device.id });
              await refresh('Sign-in revoked.');
            }, row);
        },
      );
      revoke.setAttribute('aria-describedby', date.id);
      row.append(
        date,
        element(
          'p',
          (device.remembered ? 'Remembered device' : 'Regular sign-in') +
            ' · ends by ' +
            new Date(device.expires_at * 1000).toLocaleString(),
          'hint',
        ),
        revoke,
      );
      sessions.append(row);
    }
    main.append(sessions);
    const advanced = help('Advanced access', [
      settings.encrypted
        ? 'This connection uses HTTPS encryption.'
        : 'This home-network connection uses HTTP. It does not encrypt passwords or private data.',
      'Access mode: ' +
        (settings.mode === 'https-only'
          ? 'HTTPS only. Insecure owner access is blocked.'
          : 'Home network. HTTPS is optional.'),
      'These network settings are managed by your installation configuration. Restart Inky after changing them. They do not change your birds.',
      'You can use an existing HTTPS reverse proxy, your own certificate and key, or a private VPN. A reverse proxy forwards browser requests to Inky. A VPN connects your device to your home network. If you use a private certificate authority (CA), install its trust certificate on each device first. Stop if the browser shows a certificate warning.',
    ]);
    if (settings.provider) {
      const p = settings.provider;
      advanced.append(
        element('h2', 'Single sign-on with ' + p.label),
        element(
          'p',
          'Single sign-on lets your identity service sign you in. Inky never asks for that service’s password.',
        ),
        element('p', 'Issuer: ' + p.issuer, 'hint'),
        element('p', 'Client ID: ' + p.client_id, 'hint'),
        element('p', 'Callback: ' + p.callback_url, 'hint'),
        element(
          'p',
          'Secret environment variable: ' +
            p.client_secret_env +
            '. Its value stays on the server.',
          'hint',
        ),
        element(
          'p',
          p.linked
            ? 'An owner identity is linked.'
            : 'Sign in to your provider to link your exact owner identity.',
          'connection',
        ),
      );
      if (p.ready) {
        const link = button(
          p.linked ? 'Verify or replace linked account' : 'Link my ' + p.label + ' account',
          () => sensitive(() => beginSSO('/v1/owner/oidc/link', session.remembered, true), link),
        );
        advanced.append(link);
      } else
        advanced.append(
          element(
            'p',
            p.error || 'The provider is unavailable. Ask the installer to check its settings.',
            'error',
          ),
        );
      if (p.linked && !p.identity_managed && !p.sso_only) {
        const unlink = button('Unlink single sign-on', () =>
          sensitive(async () => {
            await api('/v1/owner/oidc/unlink', {});
            await refresh('Single sign-on unlinked.');
          }, unlink),
        );
        advanced.append(unlink);
      }
      advanced.append(
        element(
          'p',
          'Signing out of Inky does not sign you out of your identity service. Its account changes do not instantly end existing Inky sessions.',
          'hint',
        ),
      );
    } else
      advanced.append(
        element(
          'p',
          'Authentik and other OpenID Connect providers are optional. Configure a provider and a fixed HTTPS callback before linking your account. Nothing is exposed to the internet automatically.',
        ),
      );
    const docs = element('a', 'Download the HTTPS, Authentik and recovery guide (.md)');
    docs.href = '/app/access-guide';
    docs.download = 'Inky-access-guide.md';
    advanced.append(docs);
    main.append(
      advanced,
      button('Back', () => {
        if (accountBackAvailable) {
          accountBackAvailable = false;
          history.back();
        } else {
          history.replaceState(null, '', '/app');
          signedIn(access).catch(showProblem);
        }
      }),
    );
  } catch (error) {
    history.replaceState(null, '', '/app');
    showProblem(error);
  }
}

function showProblem(error) {
  if (error.code === 'sign_in_required') {
    csrf = '';
    if (retainSignedOutFrame()) return;
    load('Your session ended. Sign in again.');
    return;
  }
  screen('Inky needs attention', error.message);
  main.append(
    button('Try again', () => load()),
    help('If the problem continues', [
      'Check that Inky is running and your home network or private VPN is connected. For a private-state error, check storage and permissions. Do not delete state to hide an error.',
    ]),
  );
}

async function load(notice = '') {
  main.setAttribute('aria-busy', 'true');
  try {
    const access = await api('/v1/access');
    if (!access.secure) {
      screen(
        'Open your Inky address',
        'This address is not approved for sign-in. Password entry is off.',
      );
      main.append(
        help('How to connect', [
          'Use the local address shown by your installer on your trusted home network. Use your configured HTTPS address if HTTPS-only mode is enabled.',
          'When away from home, connect through your private VPN. Do not open a router port or ignore a certificate warning.',
        ]),
        button('Check again', () => load()),
      );
      return;
    }
    if (
      access.setup_required &&
      (!access.registration_allowed || access.setup_code?.state !== 'missing')
    ) {
      screen(
        'Finish setup on the controller',
        'Create the first owner account in a private terminal on the controller. Existing bird data, display service and scheduled work stay unchanged.',
      );
      main.append(
        help(
          'For the person who installed Inky',
          [
            'Use a private terminal on the controller to choose the owner username and password. This does not change bird history or artwork. Do not delete access files to reopen browser setup.',
            'For a source checkout, prefix the command with uv run. Use the same configuration file as your existing controller.',
          ],
          'inky-bird-frame access setup --config <config.toml>',
        ),
        help(
          'If you use Docker',
          [
            'Open a private terminal on the Docker host, in the folder containing your Compose file. Run this command, choose your username and password, then check again.',
          ],
          'docker compose exec controller inky-bird-frame access setup --config /data/config.toml',
        ),
        button('Check again', () => load()),
      );
      const docs = element('a', 'Download the setup and recovery guide (.md)');
      docs.href = '/app/access-guide';
      docs.download = 'Inky-access-guide.md';
      main.append(docs);
      return;
    }
    try {
      await signedIn(access, notice);
    } catch (error) {
      if (error.code !== 'sign_in_required') throw error;
      csrf = '';
      signIn(access, notice);
    }
  } catch (error) {
    showProblem(error);
  } finally {
    main.setAttribute('aria-busy', 'false');
  }
}

const ssoResult = new URL(location.href).searchParams.get('sign_in');
if (ssoResult) history.replaceState(null, '', '/app' + location.hash);
window.addEventListener('popstate', () => {
  if (location.hash !== '#account') accountBackAvailable = false;
  load();
});
await load(
  ssoResult === 'failed'
    ? 'Single sign-on could not finish. Your existing access stays in place. Try again, or use local sign-in if available. If this continues, ask the installer to check the provider.'
    : ssoResult === 'linked'
      ? 'Single sign-on linked and verified.'
      : ssoResult === 'confirmed'
        ? 'Sign-in confirmed. Repeat the change you wanted to make.'
        : '',
);
