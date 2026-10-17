'use strict';
'require view';
'require fs';
'require ui';
'require rpc';
'require poll';

var CFG_PATH = '/opt/xiaomusic-mini/config.json';
var SVC = 'xiaomusic-mini';
var CHK = '/usr/libexec/xiaomusic-mini-check';
var INSTALLER = '/usr/libexec/xiaomusic-mini-install';
var LOG_HELPER = '/usr/libexec/xiaomusic-mini-log';
var DEFAULT_PORT = 8091;

var callSetInit = rpc.declare({ object: 'luci', method: 'setInitAction', params: [ 'name', 'action' ] });
var callInitList = rpc.declare({ object: 'luci', method: 'getInitList', params: [ 'name' ] });

function jcheck(r) {
	return r.json().catch(function () { return {}; }).then(function (j) {
		if (!r.ok || j.ok === false)
			throw new Error(j.error || ('HTTP ' + r.status));
		return j;
	});
}

function apiGet(base, path) {
	return fetch(base + path).then(jcheck);
}

function apiPost(base, path, body) {
	return fetch(base + path, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify(body || {})
	}).then(jcheck);
}

function notify(msg, type) {
	ui.addNotification(null, E('p', {}, msg), type || 'info');
}

function btn(label, fn, primary) {
	return E('button', {
		'class': 'btn cbi-button ' + (primary ? 'cbi-button-action' : 'cbi-button-neutral'),
		'click': fn
	}, label);
}

function row(title, field, desc) {
	var o = E('div', { 'class': 'cbi-value' }, [
		E('div', { 'class': 'cbi-value-title' }, title),
		E('div', { 'class': 'cbi-value-field' }, field)
	]);

	if (desc != null)
		o.appendChild(E('div', { 'class': 'cbi-value-description' }, desc));

	return o;
}

function inp(value, type) {
	var o = E('input', {
		'type': type || 'text',
		'class': 'cbi-input-text',
		'style': 'width:100%'
	});

	o.value = (value == null) ? '' : String(value);

	return o;
}

function select() {
	return E('select', { 'class': 'cbi-input-select', 'style': 'width:100%' });
}

function runChecker() {
	return fs.exec_direct(CHK, []).then(function (out) {
		return JSON.parse(out || '{}');
	}).catch(function (e) {
		return { ok: false, env_ok: false, version: null, checks: [], error: String(e.message || e) };
	});
}

return view.extend({
	load: function () {
		return Promise.all([
			fs.read(CFG_PATH).then(function (s) {
				try { return JSON.parse(s || '{}'); } catch (e) { return {}; }
			}).catch(function () { return {}; }),
			L.resolveDefault(callInitList({ name: SVC }), {}),
			runChecker()
		]);
	},

	render: function (data) {
		var cfg = (data && data[0]) || {};
		var env0 = (data && data[2]) || { ok: false, env_ok: false, checks: [] };

		var port = parseInt(cfg.http_port || DEFAULT_PORT, 10) || DEFAULT_PORT;
		var base = 'http://' + (window.location.hostname || '127.0.0.1') + ':' + port;

		var elBase = E('code', {}, base);
		var elRun = E('strong', {}, '…');
		var elEnabled = E('span', {}, '…');
		var elErr = E('div', { 'class': 'cbi-value-description', 'style': 'color:#c00;min-height:1em' }, '');
		var elVer = E('span', {}, '…');

		var elDev = E('div', {}, '…');
		var elNow = E('div', {}, '…');
		var elMeta = E('div', { 'class': 'cbi-value-description' }, '');

		var elVolNum = E('span', {}, '-');
		var elVol = E('input', { 'type': 'range', 'min': '0', 'max': '100', 'step': '1', 'style': 'flex:1' });

		var elDevices = select();
		var elList = select();
		var elSongs = E('ul', {
			'style': 'list-style:none;margin:6px 0 0;padding:0;max-height:320px;overflow:auto'
		});

		var elAcc = inp(cfg.account);
		var elPw = inp(cfg.password, 'password');
		var elDid = inp(cfg.mi_did);
		var elMusic = inp(cfg.music_dir);
		var elPort = inp(cfg.http_port || DEFAULT_PORT);
		var elHost = inp(cfg.http_host || 'auto');
		var elKw = E('textarea', {
			'class': 'cbi-input-text',
			'rows': '6',
			'style': 'width:100%;font-family:monospace'
		});

		elKw.value = JSON.stringify(cfg.key_word_dict || {}, null, 2);

		var gated = [];
		var installBusy = false;

		function mkBtn(label, fn, primary) {
			var b = btn(label, fn, primary);
			gated.push(b);
			return b;
		}

		function setGated(disabled) {
			gated.forEach(function (el) { el.disabled = !!disabled; });
			[ elVol, elList, elDevices ].forEach(function (el) { el.disabled = !!disabled; });
		}

		function doCmd(c, arg) {
			return apiPost(base, '/api/cmd', { cmd: c, arg: (arg == null ? '' : String(arg)) })
				.then(function () { return refresh(); })
				.catch(function (e) { notify('操作失败：' + e.message, 'error'); });
		}

		function refresh() {
			var apiOk = false;

			return Promise.all([
				apiGet(base, '/api/status').then(function (d) {
					apiOk = true;
					elErr.textContent = '';
					elRun.textContent = '运行中';
					elRun.style.color = '#3c8';

					elDev.textContent = (d.device_name || '（未命名设备）') +
						(d.device ? '  mi_did:' + d.device : '');
					elNow.textContent = d.cur_name || '未在播放';
					elMeta.textContent = '歌单：' + (d.cur_list || '-') +
						' · 模式：' + (d.mode || '-') +
						' · 共 ' + (d.total == null ? '-' : d.total) + ' 首';

					var lists = d.lists || [];
					var cur = Array.from(elList.options).map(function (o) { return o.value; }).join('|');
					if (cur !== lists.join('|')) {
						elList.innerHTML = '';
						lists.forEach(function (n) { elList.appendChild(E('option', { value: n }, n)); });
					}
					if (d.cur_list && lists.indexOf(d.cur_list) >= 0)
						elList.value = d.cur_list;

					if (d.volume != null && document.activeElement !== elVol) {
						elVol.value = d.volume;
						elVolNum.textContent = d.volume + '%';
					}

					if (d.device) {
						Array.from(elDevices.options).forEach(function (o) {
							if (o.value === d.device)
								elDevices.value = o.value;
						});
					}
				}).catch(function (e) {
					apiOk = false;
					elRun.textContent = '已停止 / 未响应';
					elRun.style.color = '#c00';
					elErr.textContent = '接口不可用（' + base + '）：' + e.message;
				}),
				callInitList({ name: SVC }).then(function (info) {
					var st = (info || {})[SVC];
					elEnabled.textContent = st ? (st.enabled ? '已启用' : '未启用') : '—';
				}).catch(function () { elEnabled.textContent = '—'; })
			]).then(function () { return apiOk; });
		}

		function loadSongs() {
			return apiGet(base, '/api/songs?list=' + encodeURIComponent(elList.value || '全部'))
				.then(function (d) {
					var items = d.items || [];
					elSongs.innerHTML = '';

					if (!items.length) {
						elSongs.appendChild(E('li', { 'class': 'cbi-value-description' }, '（空）'));
						return;
					}

					items.forEach(function (it, i) {
						elSongs.appendChild(E('li', {
							'style': 'padding:7px 6px;border-radius:6px;cursor:pointer',
							'click': function () { doCmd('play', it.name); }
						}, (i + 1) + '. ' + it.name));
					});
				}).catch(function (e) {
					elSongs.innerHTML = '';
					elSongs.appendChild(E('li', { 'class': 'cbi-value-description' },
						'加载歌单失败：' + e.message));
				});
		}

		function loadDevices() {
			return apiGet(base, '/api/devices').then(function (d) {
				var devs = d.devices || [];
				elDevices.innerHTML = '';

				devs.forEach(function (x) {
					var v = x.did || x.device_id || '';
					elDevices.appendChild(E('option', { value: v },
						(x.name || '未知设备') + (x.hardware ? '（' + x.hardware + '）' : '')));
					if (x.current)
						elDevices.value = v;
				});
			}).catch(function () {});
		}

		function initAction(action) {
			return callSetInit({ name: SVC, action: action }).then(function (ret) {
				if (ret && ret.error) { notify(ret.error, 'error'); return; }
				if (ret && ret.result === false) { notify('服务未执行：' + action, 'error'); return; }
				notify('已执行：' + action);
				return refresh();
			}).catch(function (e) {
				notify('服务控制失败：' + e.message, 'error');
			});
		}

		function readKw() {
			var t = String(elKw.value || '').trim();

			if (!t)
				return {};

			try {
				var o = JSON.parse(t);
				if (!o || typeof o !== 'object' || Array.isArray(o))
					throw new Error('必须是 JSON 对象');
				return o;
			} catch (e) {
				notify('自定义口令不是合法 JSON：' + e.message, 'error');
				return null;
			}
		}

		function saveCfg() {
			var port;
			var music;
			var kw;

			port = parseInt(elPort.value || DEFAULT_PORT, 10);
			if (!(port > 0 && port < 65536)) {
				notify('端口必须是 1-65535 之间的整数', 'error');
				return;
			}

			music = elMusic.value.trim();
			if (music && music.charAt(0) !== '/') {
				notify('音乐目录必须是绝对路径，例如 /mnt/sda1/music', 'error');
				return;
			}

			kw = readKw();
			if (kw === null)
				return;

			var merged = {};
			Object.keys(cfg).forEach(function (k) { merged[k] = cfg[k]; });

			merged.account = elAcc.value.trim();
			merged.password = elPw.value;
			merged.mi_did = elDid.value.trim();
			merged.music_dir = music;
			merged.http_port = port;
			merged.http_host = elHost.value.trim() || 'auto';

			if (Object.keys(kw).length)
				merged.key_word_dict = kw;
			else
				delete merged.key_word_dict;

			// 首次保存（配置为空）时补齐后端必需项
			if (!merged.token_path)
				merged.token_path = '/opt/xiaomusic-mini/mi.token';
			if (!merged.log_file)
				merged.log_file = '/opt/xiaomusic-mini/xiaomusic-mini.log';
			if (!merged.poll_sec)
				merged.poll_sec = 1.5;
			if (!merged.fuzzy_cutoff)
				merged.fuzzy_cutoff = 0.6;
			if (!merged.use_music_audio_id)
				merged.use_music_audio_id = '1582971365183456177';

			cfg = merged;

			return fs.write(CFG_PATH, JSON.stringify(merged, null, 2), 384).then(function () {
				notify('配置已保存，正在重启服务…');
				return initAction('restart');
			}).then(function () {
				base = 'http://' + (window.location.hostname || '127.0.0.1') + ':' + port;
				elBase.textContent = base;
				return checkAgain().then(function () {
					return Promise.all([ refresh(), loadDevices(), loadSongs() ]);
				});
			}).catch(function (e) {
				notify('保存配置失败：' + e.message, 'error');
			});
		}

		function applyEnv(res) {
			elEnvBox.innerHTML = '';
			elVer.textContent = res.version ? ('v' + res.version) : '—';

			var checks = res.checks || [];
			var failed = checks.filter(function (c) { return !c.ok; });

			if (!checks.length) {
				envDetails.open = true;
				elEnvState.textContent = '检测失败';
				elEnvState.style.color = '#c00';
				elEnvBanner.style.display = '';
				elEnvBanner.textContent = '环境检测失败：' + (res.error || '无法执行检测脚本') +
					'。请确认 /usr/libexec/xiaomusic-mini-check 已安装且可执行。';
				elEnvOk.textContent = '检测失败';
				elEnvOk.style.color = '#c00';
				setGated(true);
				return;
			}

			elEnvState.textContent = failed.length
				? (failed.length + ' 项未通过')
				: '全部通过';
			elEnvState.style.color = failed.length ? '#c00' : '#3c8';

			// 有未通过项时自动展开，避免折叠后看不到问题
			if (failed.length)
				envDetails.open = true;

			elEnvBanner.style.display = failed.length ? '' : 'none';
			elEnvBanner.textContent = res.env_ok
				? '配置尚未完成：请填写小米账号/密码，并确认音乐目录存在，保存后生效。'
				: '运行依赖不完整，服务无法启动。请先安装下方缺失项，安装完成后会自动重新检测。';

			elEnvOk.textContent = failed.length
				? ('未通过 ' + failed.length + ' 项检查')
				: '环境与配置正常，可以运行';
			elEnvOk.style.color = failed.length ? '#c00' : '#3c8';

			checks.forEach(function (c) {
				var acts = [];

				if (!c.ok && (c.fix || []).length) {
					acts.push(E('button', {
						'class': 'btn cbi-button cbi-button-action',
						'click': function () { confirmInstall(c); }
					}, '一键安装'));
					(c.fix || []).forEach(function (cmd) {
						acts.push(E('code', {}, cmd));
					});
				}

				elEnvBox.appendChild(E('div', {
					'style': 'display:flex;gap:10px;align-items:flex-start;padding:6px 0;border-bottom:1px dashed #eee'
				}, [
					E('span', {
						'style': 'width:1.4em;font-weight:700;color:' + (c.ok ? '#3c8' : '#c00')
					}, c.ok ? '✓' : '✗'),
					E('span', { 'style': 'min-width:8.5em' }, c.label),
					E('span', {
						'class': 'cbi-value-description',
						'style': 'flex:1;min-width:8em'
					}, c.detail || (c.ok ? '已满足' : '未满足')),
					E('span', {
						'style': 'display:flex;gap:6px;align-items:center;flex-wrap:wrap;max-width:52%'
					}, acts)
				]));
			});

			setGated(!res.env_ok);
		}

		function checkAgain() {
			return runChecker().then(function (res) {
				applyEnv(res);
				return res;
			});
		}

		function confirmInstall(c) {
			ui.showModal('安装依赖：' + c.label, [
				E('p', {}, '将在后台执行安装（需要联网），可能需要 1-3 分钟：'),
				E('pre', { 'style': 'margin:6px 0;white-space:pre-wrap' }, (c.fix || []).join('\n')),
				E('p', { 'class': 'cbi-value-description' },
					'安装完成后页面自动重新检测；也可在 SSH 中手动执行上面的命令。'),
				E('div', { 'class': 'right' }, [
					E('button', { 'class': 'btn cbi-button', 'click': ui.hideModal }, '取消'),
					' ',
					E('button', {
						'class': 'btn cbi-button cbi-button-action',
						'click': function () {
							ui.hideModal();
							startInstall(c.id);
						}
					}, '开始安装')
				])
			]);
		}

		function startInstall(id) {
			return fs.exec_direct(INSTALLER, [ id ]).then(function (out) {
				if (String(out || '').indexOf('started') < 0)
					throw new Error(String(out || '').trim() || '安装器未启动');
				installBusy = true;
				notify('安装任务已启动（' + id + '），完成后自动重新检测…');
			}).catch(function (e) {
				notify('启动安装失败：' + (e.message || e), 'error');
			});
		}

		function pollInstall() {
			if (!installBusy)
				return;

			return fs.exec_direct(INSTALLER, [ '--status' ]).then(function (out) {
				var st = String(out || '').trim();

				if (/^running\b/.test(st))
					return;

				installBusy = false;
				notify('安装任务结束：' + st, /^done\s+\S+\s+0$/.test(st) ? 'info' : 'error');

				return checkAgain().then(function () { return refresh(); });
			}).catch(function (e) {
				installBusy = false;
				notify('读取安装状态失败：' + (e.message || e), 'error');
			});
		}

		function showDeps() {
			return fs.exec_direct(CHK, [ '--deps' ]).then(function (out) {
				var txt = String(out || '').trim();

				ui.showModal('依赖安装命令', [
					E('p', {}, txt
						? '可在 SSH 中逐条执行（或返回上一页用「一键安装」）：'
						: '当前没有缺失的依赖。'),
					E('pre', { 'style': 'margin:6px 0;white-space:pre-wrap' }, txt || '（无）'),
					E('div', { 'class': 'right' }, [
						E('button', { 'class': 'btn cbi-button', 'click': ui.hideModal }, '关闭')
					])
				]);
			}).catch(function (e) {
				notify('读取安装命令失败：' + (e.message || e), 'error');
			});
		}

		function showLog() {
			return fs.exec_direct(LOG_HELPER, [ '200' ]).then(function (out) {
				ui.showModal('运行日志（最近 200 行）', [
					E('pre', {
						'style': 'max-height:60vh;overflow:auto;white-space:pre-wrap;font-size:12px'
					}, out || '（空）'),
					E('div', { 'class': 'right' }, [
						E('button', { 'class': 'btn cbi-button', 'click': ui.hideModal }, '关闭')
					])
				]);
			}).catch(function (e) {
				notify('读取日志失败：' + (e.message || e), 'error');
			});
		}

		function showInstallLog() {
			return fs.exec_direct(INSTALLER, [ '--log' ]).then(function (out) {
				ui.showModal('依赖安装日志（最近 200 行）', [
					E('pre', {
						'style': 'max-height:60vh;overflow:auto;white-space:pre-wrap;font-size:12px'
					}, out || '（空）'),
					E('div', { 'class': 'right' }, [
						E('button', { 'class': 'btn cbi-button', 'click': ui.hideModal }, '关闭')
					])
				]);
			}).catch(function (e) {
				notify('读取安装日志失败：' + (e.message || e), 'error');
			});
		}

		var elEnvBanner = E('div', {
			'style': 'display:none;background:#fbeaea;color:#c00;padding:10px 12px;border-radius:6px;margin:6px 0 10px'
		}, '');
		var elEnvOk = E('strong', {}, '…');
		var elEnvBox = E('div', {});
		var elEnvState = E('span', { 'class': 'cbi-value-description', 'style': 'margin-left:10px;font-weight:400' }, '');

		// 环境检测默认折叠；有未通过项时自动展开。手动展开/收起的状态记在浏览器里。
		var ENV_PREF_KEY = 'xiaomusic-mini-env-open';
		var envPrefOpen = null;
		try { envPrefOpen = window.localStorage.getItem(ENV_PREF_KEY); } catch (e) {}

		var envDetails = E('details', {
			'class': 'cbi-section',
			'style': 'padding:0;border:1px solid #e2e2e2;border-radius:8px;margin-bottom:14px'
		}, E('summary', {
			'style': 'cursor:pointer;padding:12px 14px;font-weight:600;font-size:1.05em;user-select:none;outline:none'
		}, [ '环境检测', elEnvState ]));

		envDetails.appendChild(E('div', { 'style': 'padding:0 14px 12px' }, [
			elEnvBanner,
			row('检测结果', E('div', {}, [ elEnvOk, ' ', E('span', { 'class': 'cbi-value-description' }, '后端'), ' ', elVer ])),
			row('明细', elEnvBox),
			row('操作', E('div', {}, [
				btn('重新检测', function () {
					var b = this;
					b.disabled = true;
					b.textContent = '检测中…';
					checkAgain().then(function () {
						b.disabled = false;
						b.textContent = '重新检测';
					});
				}, true),
				' ',
				btn('查看安装命令', showDeps),
				' ',
				btn('安装日志', showInstallLog)
			]))
		]));

		envDetails.open = (envPrefOpen === '1') || !(env0 && env0.ok === true);
		envDetails.addEventListener('toggle', function () {
			try { window.localStorage.setItem(ENV_PREF_KEY, envDetails.open ? '1' : '0'); } catch (e) {}
		});

		elVol.addEventListener('input', function () { elVolNum.textContent = elVol.value + '%'; });
		elVol.addEventListener('change', function () { doCmd('volume', elVol.value); });
		elList.addEventListener('change', function () { loadSongs(); });
		elDevices.addEventListener('change', function () {
			doCmd('device', elDevices.value).then(function () { return loadDevices(); });
		});

		var dom = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, '小爱音乐 Mini'),
			E('p', { 'class': 'cbi-value-description' },
				'小爱音箱语音点播本地音乐：后端（xiaomusic-mini）随本应用安装，语音口令由音箱轮询解析。接口地址：'),
			row('', elBase, '若 LuCI 以 https 访问，浏览器会拦截 http:// 接口，请改用 http:// 打开本页。'),

			envDetails,

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '服务'),
				row('运行状态', elRun),
				row('开机自启', elEnabled),
				row('状态提示', elErr),
				row('操作', E('div', {}, [
					mkBtn('启动', function () { initAction('start'); }),
					' ', mkBtn('停止', function () { initAction('stop'); }),
					' ', mkBtn('重启', function () { initAction('restart'); }, true),
					' ', mkBtn('启用自启', function () { initAction('enable'); }),
					' ', mkBtn('禁用自启', function () { initAction('disable'); }),
					' ', btn('查看日志', showLog)
				]))
			]),

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '播放控制'),
				row('当前设备', elDev),
				row('正在播放', elNow, elMeta),
				row('音量', E('div', {
					'style': 'display:flex;align-items:center;gap:10px'
				}, [ elVolNum, elVol ])),
				row('控制', E('div', {}, [
					mkBtn('上一首', function () { doCmd('prev'); }),
					' ', mkBtn('播放/继续', function () { doCmd('toggle'); }, true),
					' ', mkBtn('下一首', function () { doCmd('next'); }),
					' ', mkBtn('停止', function () { doCmd('stop'); }),
					' ', mkBtn('刷新歌单', function () { doCmd('rescan').then(loadSongs); })
				])),
				row('选择音箱', elDevices)
			]),

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '歌单'),
				row('歌单', elList),
				E('div', { 'class': 'cbi-value-field' }, elSongs)
			]),

			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '配置'),
				row('小米账号', elAcc),
				row('密码', elPw),
				row('目标 mi_did', elDid, '留空则自动选择 OH2 系列音箱'),
				row('音乐目录', elMusic, '支持子目录作为歌单；建议放在外接存储，如 /mnt/sda1/music'),
				row('控制台端口', elPort, '音乐文件服务与 HTTP API 端口，默认 8091'),
				row('控制台主机', elHost, 'auto 表示自动探测本机 LAN IP（OpenWrt lan 接口），失败再回退默认网关；双 NAT 环境务必确认音箱能访问该地址'),
				row('自定义口令', elKw, 'JSON 对象，如 {"测试自定义口令": "exec#code1(\\"hello\\")"}；' +
					'函数来自后端 plugins 目录（/usr/lib/xiaomusic-mini/plugins 或 /opt/xiaomusic-mini/plugins）'),
				row('', E('div', {}, [ btn('保存并重启', saveCfg, true) ]))
			])
		]);

		poll.add(function () { return refresh(); }, 3);
		poll.add(pollInstall, 3);
		poll.start();

		applyEnv(env0);

		refresh().then(function () {
			return loadDevices();
		}).then(function () {
			return loadSongs();
		});

		return dom;
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
