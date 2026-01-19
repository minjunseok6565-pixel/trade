// LLM 관련 상태
let isLLMLoading = false;

function setLLMLoadingStatus(kind, loading, message) {
  if (!llmStatus) return;

  if (loading) {
    llmStatus.textContent = message || 'LLM 처리 중...';
    if (homeLLMOutput && kind === 'main') {
      homeLLMOutput.textContent = llmStatus.textContent;
    }
  } else {
    llmStatus.textContent = '';
  }
}

// 퍼스트 메시지 로딩
function showFirstMessageForSelectedTeam() {
  const team = appState.selectedTeam;
  if (!team) return;

  const teamId = team.id;

  // 이미 이 팀에 대해 퍼스트 메시지를 보여줬다면 다시 안 함
  if (appState.firstMessageShownTeams && appState.firstMessageShownTeams[teamId]) {
    return;
  }

  // /static/NBA.html에서 불릴 때 기준 경로:
  //   /static/prompt/first_messages/<teamId>.txt
  // 같은 식으로 정적 파일을 놓았다고 가정
  const url = `/static/prompt/first_messages/${teamId}.txt`;

  fetch(url)
    .then(res => {
      if (!res.ok) {
        console.warn('퍼스트 메시지 텍스트를 찾지 못했습니다:', url);
        return null;
      }
      return res.text();
    })
    .then(text => {
      if (!text) return;
      const trimmed = text.trim();
      homeLLMOutput.textContent = trimmed;

      // 한번 보여줬다고 기록
      appState.firstMessageShownTeams[teamId] = true;

      // 나중에 대화 히스토리 기능 붙일 때를 대비해서,
      // 존재하면 히스토리에 같이 넣어두면 좋다 (없으면 무시됨)
      if (Array.isArray(appState.chatHistory)) {
        appState.chatHistory.push({ role: 'assistant', text: trimmed });
      }
    })
    .catch(err => {
      console.warn('퍼스트 메시지 로드 중 오류:', err);
    });
}

/* 메인 탭 */

// 메인 LLM 호출
async function sendToMainLLM() {
  if (isLLMLoading) return;
  if (!appState.apiKey) {
    alert('먼저 상단에서 Gemini API 키를 입력해주세요.');
    return;
  }
  const userInput = homeUserInput.value.trim();
  if (!userInput) {
    alert('질문 또는 지시를 입력해주세요.');
    return;
  }

  isLLMLoading = true;
  const simIntent = detectSimulationIntent(userInput);
  setLLMLoadingStatus(
    'main',
    true,
    simIntent.shouldSimulate
      ? '경기 시뮬레이션 및 브리핑 중...'
      : 'LLM 응답 생성 중...'
  );
  btnSendToLLM.disabled = true;

  try {
    // 대화 히스토리에 유저 메시지 추가
    appState.chatHistory = appState.chatHistory || [];
    appState.chatHistory.push({ role: 'user', text: userInput });

    // 메인 LLM 컨텍스트 생성
    const context = buildContextForLLM();

    // 사용자가 시뮬레이션을 요청했다면 먼저 매치 엔진을 돌린다.
    let augmentedUserInput = userInput;
    let simulatedGames = [];
    if (simIntent.shouldSimulate) {
      simulatedGames = await runLLMSimulationWorkflow(simIntent.gamesToSimulate);

      if (simulatedGames.length > 0) {
        context.simulatedGames = simulatedGames;
        const summaryText = formatSimulationSummary(simulatedGames);
        augmentedUserInput =
          `${userInput}\n\n` +
          '[시뮬레이션 결과]\n' +
          `${summaryText}\n` +
          '위 결과를 바탕으로 핵심 하이라이트와 관전 포인트를 요약해서 브리핑해줘.';
      } else {
        augmentedUserInput =
          `${userInput}\n\n` +
          '[시뮬레이션 실패] 실제 경기를 실행하지 못했습니다. 가능한 원인을 알려주세요.';
      }
    }

    const payload = {
      apiKey: appState.apiKey,
      mainPrompt: (mainPromptTextarea?.value || '').trim(),
      // 백엔드 Pydantic 모델이 userMessage(alias) 필드를 요구하므로 두 키 모두 채운다.
      userInput: augmentedUserInput,
      userMessage: augmentedUserInput,
      context: JSON.stringify(context)
    };

    const res = await fetch('/api/chat-main', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });

    if (!res.ok) {
      console.error('메인 LLM API 에러:', await res.text());
      alert('메인 LLM 호출 중 문제가 발생했습니다.');
      return;
    }

    const data = await res.json();
    const answer = (data.reply || data.answer || '').trim();
    homeLLMOutput.textContent = answer || '(빈 응답)';

    // 어시스턴트 응답도 히스토리에 추가
    appState.chatHistory.push({ role: 'assistant', text: answer });

  } catch (err) {
    console.error('sendToMainLLM 오류:', err);
    alert('LLM 호출 중 오류가 발생했습니다.');
  } finally {
    isLLMLoading = false;
    setLLMLoadingStatus('main', false);
    btnSendToLLM.disabled = false;
    homeUserInput.value = '';
  }
}

// 메인 LLM 컨텍스트 구성
function buildContextForLLM() {
  const team = appState.selectedTeam;
  const schedule = appState.cachedViews.schedule || {};
  const scores = appState.cachedViews.scores || {};

  // 간단한 팀/리그 상태 요약을 만들 수 있다.
  const teamName = team?.name || '(선택된 팀 없음)';
  const latestGames = (scores.games || []).slice(0, 5);

  const latestGamesText = latestGames
    .map(g => {
      const homeName =
        TEAMS.find(t => t.id === g.home_team_id)?.name || g.home_team_id;
      const awayName =
        TEAMS.find(t => t.id === g.away_team_id)?.name || g.away_team_id;
      const scoreText =
        g.home_score != null && g.away_score != null
          ? `${g.home_score} - ${g.away_score}`
          : '-';
      return `[${g.date}] ${homeName} vs ${awayName} : ${scoreText}`;
    })
    .join('\n');

  // 간단한 히스토리 (최근 N턴만 사용)
  const history = (appState.chatHistory || []).slice(-8);

  return {
    selectedTeamId: team?.id || null,
    selectedTeamName: teamName,
    currentDate: appState.currentDate,
    progressTurns: appState.progressTurns,
    latestGames: latestGamesText,
    history
  };
}

function detectSimulationIntent(message) {
  const lower = (message || '').toLowerCase();
  const hasSimKeyword =
    lower.includes('시뮬') ||
    lower.includes('시뮬레이션') ||
    lower.includes('simulate') ||
    lower.includes('경기 진행') ||
    lower.includes('게임 진행') ||
    lower.includes('매치 엔진');

  if (!hasSimKeyword) {
    return { shouldSimulate: false, gamesToSimulate: 0 };
  }

  const numberMatch = message.match(/(\d+)\s*(경기|게임|matches?|games?)/i);
  const gamesToSimulate = numberMatch ? Math.max(1, parseInt(numberMatch[1], 10)) : 1;

  return { shouldSimulate: true, gamesToSimulate };
}

async function runLLMSimulationWorkflow(gamesToSimulate) {
  const results = [];

  for (let i = 0; i < gamesToSimulate; i += 1) {
    const result = await simulateGameProgress();
    if (!result || result.success === false) {
      break;
    }
    results.push(result);
  }

  return results;
}

function formatSimulationSummary(simulatedGames) {
  return simulatedGames
    .map(g => {
      const matchup = `${g.home_team_name} ${g.home_score} - ${g.away_score} ${g.away_team_name}`;
      const outcome = g.result_for_user_team === 'W' ? '승리' : '패배';
      return `${g.game_date} · ${matchup} (${outcome})`;
    })
    .join('\n');
}

// 이벤트 바인딩: 메인 LLM 호출
if (typeof btnSendToLLM !== 'undefined' && btnSendToLLM) {
  btnSendToLLM.addEventListener('click', sendToMainLLM);
}

// 이벤트 바인딩: 빠른 경기 시뮬레이션
if (typeof btnSimGame !== 'undefined' && btnSimGame) {
  btnSimGame.addEventListener('click', async () => {
    if (!appState.selectedTeam) {
      alert('먼저 팀을 선택한 뒤 진행하세요.');
      return;
    }

    btnSimGame.disabled = true;
    const originalText = btnSimGame.textContent;
    btnSimGame.textContent = '경기 시뮬레이션 중...';

    try {
      await simulateGameProgress();
    } finally {
      btnSimGame.disabled = false;
      btnSimGame.textContent = originalText;
    }
  });
}

// 로스터 불러오기
async function loadRosterForTeam(teamId) {
  if (!teamId) return;
  if (appState.rosters[teamId]) return;

  try {
    const res = await fetch(`/api/roster-summary/${teamId}`);
    if (!res.ok) {
      console.warn('로스터 요약 불러오기 실패:', await res.text());
      return;
    }
    const data = await res.json();
    appState.rosters[teamId] = data;
  } catch (e) {
    console.warn('로스터 요약 불러오기 오류:', e);
  }
}

