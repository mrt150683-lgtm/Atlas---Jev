/* Local session and project binding for every Atlas page. */
(() => {
  const session = window.ATLAS_SESSION;
  if (!session) return;
  const original = window.fetch.bind(window);
  let stale = false;
  window.fetch = async (input, options = {}) => {
    const url = new URL(input instanceof Request ? input.url : input, location.href);
    if (url.origin !== location.origin || !url.pathname.startsWith('/api/')) return original(input, options);
    const headers = new Headers(input instanceof Request ? input.headers : undefined);
    new Headers(options.headers).forEach((v, k) => headers.set(k, v));
    headers.set('X-Atlas-Session', session.token);
    headers.set('X-Atlas-Project', session.project);
    const response = await original(input, {...options, headers});
    const projectChanged = response.status === 409 && (await response.clone().json().catch(() => ({}))).code === 'project_changed';
    if (projectChanged && !stale) {
      stale = true;
      const banner = document.createElement('div');
      banner.setAttribute('role', 'alert');
      banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:99999;background:#613e12;color:white;padding:16px;text-align:center;font:14px system-ui';
      banner.append('The active project changed. Reload to continue safely. ');
      const button = document.createElement('button');
      button.textContent = 'Reload project'; button.onclick = () => location.reload();
      banner.append(button); document.body.append(banner);
    }
    return response;
  };
})();
