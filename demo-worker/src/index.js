export default {
  async fetch(request, env) {
    if (request.method === 'POST') {
      return new Response('ok\n', { headers: { 'Access-Control-Allow-Origin': '*' } });
    }
    if (request.method === 'OPTIONS') {
      return new Response(null, { headers: { 'Access-Control-Allow-Origin': '*', 'Access-Control-Allow-Headers': '*' } });
    }

    const upgrade = request.headers.get('Upgrade');
    if (upgrade !== 'websocket') {
      return new Response('herdr-remote demo relay. Connect via WebSocket.', {
        headers: { 'Access-Control-Allow-Origin': '*' }
      });
    }

    const [client, server] = Object.values(new WebSocketPair());
    server.accept();

    const agents = [
      { pane_id: 'demo:1', agent: 'claude', status: 'working', project: 'phoenix-api', cwd: '/dev/phoenix-api', host: 'local' },
      { pane_id: 'demo:2', agent: 'codex', status: 'idle', project: 'nova-ingest', cwd: '/dev/nova-ingest', host: 'local' },
      { pane_id: 'demo:3', agent: 'kiro', status: 'blocked', project: 'orbit-ui', cwd: '/dev/orbit-ui', host: 'local' },
      { pane_id: 'demo:4', agent: 'grok', status: 'working', project: 'atlas-core', cwd: '/dev/atlas-core', host: 'remote-1' },
      { pane_id: 'demo:5', agent: 'copilot', status: 'idle', project: 'delta-sync', cwd: '/dev/delta-sync', host: 'local' },
      { pane_id: 'demo:6', agent: 'claude', status: 'working', project: 'nebula-ml', cwd: '/dev/nebula-ml', host: 'remote-2' },
    ];

    const blockedPrompt = `Do you want to allow this tool call?\n\nTool: write_file\nPath: src/components/Graph.tsx\n\n> yes, single permission\n> trust, always allow\n> no (tab to edit)`;
    const projects = [
      { id: 'phoenix-api', name: 'phoenix-api', description: 'Demo API workspace', group: 'Demo', tabs: ['terminal', 'agent'], active: true, workspace_id: '', has_agent: true, agent: 'claude' },
      { id: 'orbit-ui', name: 'orbit-ui', description: 'Demo frontend workspace', group: 'Demo', tabs: ['terminal', 'agent'], active: true, workspace_id: '', has_agent: true, agent: 'codex' },
      { id: 'starlight-docs', name: 'starlight-docs', description: 'Available project template', group: 'Demo', tabs: ['terminal', 'agent'], active: false, workspace_id: '', has_agent: false, agent: '' },
    ];

    server.send(JSON.stringify({ type: 'agents', agents }));
    server.send(JSON.stringify({
      type: 'blocked', pane_id: 'demo:3', agent: 'kiro', project: 'orbit-ui',
      prompt: blockedPrompt, host: 'local',
      options: ['yes, single permission', 'trust, always allow', 'no (tab to edit)']
    }));

    let interval = setInterval(() => {
      const idx = Math.floor(Math.random() * agents.length);
      const statuses = ['working', 'idle', 'blocked'];
      agents[idx].status = statuses[Math.floor(Math.random() * statuses.length)];
      try {
        server.send(JSON.stringify({ type: 'agents', agents }));
        if (agents[idx].status === 'blocked') {
          server.send(JSON.stringify({
            type: 'blocked', pane_id: agents[idx].pane_id, agent: agents[idx].agent,
            project: agents[idx].project, prompt: blockedPrompt, host: agents[idx].host,
            options: ['yes, single permission', 'trust, always allow', 'no (tab to edit)']
          }));
        }
      } catch { clearInterval(interval); }
    }, 5000);

    server.addEventListener('message', (event) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.type === 'read_pane') {
          server.send(JSON.stringify({
            type: 'pane_content', pane_id: msg.pane_id,
            content: `$ herdr agent session\n\n[demo mode -- read-only preview]\n\nAgent: ${msg.pane_id.split(':')[1]}\nProject: ${agents.find(a => a.pane_id === msg.pane_id)?.project || 'unknown'}\n\n  Compiled successfully\n  Running tests...\n\n  PASS src/index.test.ts\n  PASS src/utils.test.ts\n\nAll tests passed.`,
            ...(msg.request_id ? {request_id: msg.request_id} : {})
          }));
        } else if (msg.type === 'list_projects') {
          server.send(JSON.stringify({
            type:'projects', projects, agents:[
              {id:'codex', label:'Codex'}, {id:'claude', label:'Claude'}
            ], request_id:msg.request_id
          }));
        } else if (msg.type === 'activate_project') {
          server.send(JSON.stringify({
            type:'project_activated', ok:false, project_id:msg.project_id,
            request_id:msg.request_id, error:'Project launching is unavailable in demo mode.'
          }));
        } else if (msg.type === 'close_project') {
          server.send(JSON.stringify({
            type:'project_closed', ok:false, project_id:msg.project_id,
            request_id:msg.request_id, error:'Closing projects is unavailable in demo mode.'
          }));
        } else if (msg.type === 'respond') {
          const a = agents.find(x => x.pane_id === msg.pane_id);
          if (a) a.status = 'working';
          server.send(JSON.stringify({
            type: 'command_result', action: 'respond', ok: Boolean(a),
            pane_id: msg.pane_id, request_id: msg.request_id
          }));
          server.send(JSON.stringify({ type: 'agents', agents }));
        } else if (msg.type === 'submit_text' || msg.type === 'send_text' || msg.type === 'send_keys') {
          server.send(JSON.stringify({
            type: 'command_result', action: msg.type, ok: agents.some(a => a.pane_id === msg.pane_id),
            pane_id: msg.pane_id, request_id: msg.request_id
          }));
        }
      } catch {}
    });

    server.addEventListener('close', () => clearInterval(interval));
    return new Response(null, { status: 101, webSocket: client });
  }
};
