// Game_sims.js
// 경기 스케줄/시뮬레이션 전담 모듈 (신버전 엔드포인트만 사용)
// - /api/team-schedule
// - /api/advance-league
// - /api/simulate-game
//
// 전역으로 존재하는 것들에 의존함:
//   appState, TEAMS, seasonDateLabel, progressLabel,
//   homeLog, homeLLMOutput, renderAllTabs, renderSidebarRecentGames, callSubLLMStateUpdate

// 다음에 치를 경기 찾기 (아직 점수가 없는 첫 경기)
function getNextScheduledGame() {
  const schedule = appState.cachedViews.schedule;
  if (!schedule.games || schedule.games.length === 0) return null;

  for (let i = 0; i < schedule.games.length; i++) {
    const g = schedule.games[i];
    if (g.home_score == null && g.away_score == null) {
      schedule.currentIndex = i;
      return g;
    }
  }
  return null; // 시즌 종료
}

// 이전 경기 이후 쉬는 날 수 계산
function computeRestDaysForUserTeam() {
  const schedule = appState.cachedViews.schedule;
  if (!schedule.games || schedule.games.length === 0) return 0;

  const teamId = schedule.teamId;
  const currentIndex = schedule.currentIndex ?? 0;

  let lastGameDate = null;

  // currentIndex 이전에서 우리 팀이 뛴 마지막 경기 날짜를 찾는다
  for (let i = currentIndex - 1; i >= 0; i--) {
    const g = schedule.games[i];
    if (!g) continue;
    if (g.home_team_id === teamId || g.away_team_id === teamId) {
      lastGameDate = g.date;
      break;
    }
  }

  if (!lastGameDate) {
    // 아직 시즌 첫 경기
    return 3; // 그냥 여유 있게 3일 쉰 걸로 가정
  }

  const currentGame = schedule.games[currentIndex];
  if (!currentGame) return 0;

  const d1 = new Date(lastGameDate);
  const d2 = new Date(currentGame.date);
  const diffMs = d2 - d1;
  const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

  // 이전 경기 다음 날을 기준으로 계산하므로, diffDays - 1이 "쉬는 날 수"
  const restDays = Math.max(0, diffDays - 1);
  return restDays;
}

// 쉬는 날 수 → 피로도 계수로 변환
function calcFatigueFactor(restDays) {
  if (restDays <= 0) return 0.92;  // 백투백: 꽤 피곤
  if (restDays === 1) return 0.97; // 하루 쉼
  if (restDays === 2) return 1.0;  // 기준
  if (restDays === 3) return 1.03; // 상쾌
  return 1.05;                     // 4일 이상 푹 쉼
}

// 팀 전술 객체 생성 (matchengine_v3 기대 포맷)
function buildTacticsForTeam(teamId, fatigueFactor) {
  const userTeam = appState.selectedTeam;
  const isUserTeam = userTeam && userTeam.id === teamId;

  if (isUserTeam) {
    const tactics = getOrCreateTacticsForTeam(teamId);
    const normalizeWeight = (value, fallback = 5) => {
      const raw = value ?? fallback;
      const numeric = Number.isFinite(raw) ? raw : fallback;
      return Math.max(0.2, numeric / 5);
    };
    return {
      pace: tactics.pace ?? 0,
      offense_scheme: tactics.offenseScheme || 'Spread_HeavyPnR',
      defense_scheme: tactics.defenseScheme || 'Drop',
      scheme_weight_sharpness: normalizeWeight(tactics.offensePrimaryWeight, 5),
      scheme_outcome_strength: normalizeWeight(tactics.offenseSecondaryWeight, 5),
      def_scheme_weight_sharpness: normalizeWeight(tactics.defensePrimaryWeight, 5),
      def_scheme_outcome_strength: normalizeWeight(tactics.defenseSecondaryWeight, 5),
      rotation_size: tactics.rotationSize || 9,
      lineup: {
        starters: tactics.starters || [],
        bench: tactics.bench || []
      },
      minutes: tactics.minutes || {}
    };
  }

  // 상대 팀은 기본값(페이스/피로만) 전달
  return {
    pace: 0
  };
}

// 시즌 스케줄을 서버에서 받아오기 (신버전: /api/team-schedule/{teamId})
async function generateSeasonSchedule(teamId) {
  const schedule = appState.cachedViews.schedule;

  // 이미 같은 팀 스케줄이 로드되어 있으면 다시 불러오지 않는다.
  if (schedule.teamId === teamId && schedule.games && schedule.games.length > 0) {
    return;
  }

  schedule.teamId = teamId;
  schedule.games = [];
  schedule.currentIndex = 0;

  try {
    const res = await fetch(`/api/team-schedule/${teamId}`);
    if (!res.ok) {
      console.error("팀 스케줄 로드 실패:", await res.text());
      alert("시즌 스케줄을 불러오는 중 문제가 발생했습니다.");
      return;
    }

    const data = await res.json();
    const games = data.games || [];

    // 그대로 schedule.games에 옮김
    schedule.games = games.map(g => ({
      game_id: g.game_id,
      date: g.date,
      home_team_id: g.home_team_id,
      away_team_id: g.away_team_id,
      home_score: g.home_score,
      away_score: g.away_score,
      result_for_user_team: g.result_for_user_team ?? null
    }));

    // 아직 안 치른 첫 경기 위치
    const idx = schedule.games.findIndex(
      g => g.home_score == null && g.away_score == null
    );
    schedule.currentIndex = idx === -1 ? schedule.games.length - 1 : idx;

    // Scores 뷰(최근 경기) 업데이트
    const finished = schedule.games
      .filter(g => g.home_score != null && g.away_score != null)
      .sort((a, b) => (a.date < b.date ? 1 : -1));

    appState.cachedViews.scores.latest_date = finished[0]?.date || null;
    appState.cachedViews.scores.games = finished.slice(0, 50);

    // 시즌 시작 날짜를 현재 인게임 날짜로 초기화
    if (schedule.games.length > 0) {
      appState.currentDate = schedule.games[0].date;
      if (typeof seasonDateLabel !== "undefined" && seasonDateLabel) {
        seasonDateLabel.textContent = appState.currentDate;
      }
    }
  } catch (err) {
    console.error("팀 스케줄 로드 중 오류:", err);
    alert("시즌 스케줄을 불러오는 중 오류가 발생했습니다.");
  }
}

async function requestSeasonReportForUserTeam() {
  const teamId = appState.selectedTeam?.id || appState.cachedViews.schedule?.teamId || TEAMS[0]?.id;

  if (!appState.apiKey) {
    alert("먼저 상단에서 Gemini API 키를 입력해주세요.");
    return null;
  }

  if (!teamId) {
    alert("팀 정보를 찾을 수 없습니다. 시즌 결산을 진행할 수 없습니다.");
    return null;
  }

  if (typeof homeLLMOutput !== "undefined" && homeLLMOutput) {
    homeLLMOutput.textContent = "시즌 결산 리포트를 생성하는 중입니다...";
  }

  try {
    const res = await fetch("/api/season-report", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        apiKey: appState.apiKey,
        user_team_id: teamId
      })
    });

    if (!res.ok) {
      console.error("시즌 결산 API 에러:", await res.text());
      alert("시즌 결산 리포트 생성에 실패했습니다. 콘솔을 확인해주세요.");
      return null;
    }

    const data = await res.json();
    const reportText = (
      data.report_markdown ||
      data.report ||
      data.text ||
      ""
    ).trim();

    if (typeof homeLLMOutput !== "undefined" && homeLLMOutput) {
      homeLLMOutput.textContent = reportText || "(빈 리포트)";
    }

    if (typeof handleSeasonReportGenerated === "function") {
      handleSeasonReportGenerated(reportText);
    }

    return reportText;
  } catch (err) {
    console.error("시즌 결산 리포트 생성 중 오류:", err);
    alert("시즌 결산 리포트 생성 중 오류가 발생했습니다.");
    return null;
  }
}

// 메인 경기 시뮬레이션 함수 (신버전 엔드포인트만 사용)
async function simulateGameProgress() {
  const userTeam = appState.selectedTeam || TEAMS[0];
  const schedule = appState.cachedViews.schedule;

  if (!schedule.teamId) {
    schedule.teamId = userTeam.id;
  }

  if (!schedule.games || schedule.games.length === 0) {
    await generateSeasonSchedule(schedule.teamId);
  }

  const nextGame = getNextScheduledGame();
  if (!nextGame) {
    // 1) 시즌 종료 알림
    alert("더 이상 남은 정규시즌 경기가 없습니다.");

    // 2) 시즌 결산 안내 알림
    alert("시즌 결산에 돌입합니다.");

    // 3) 시즌 결산 리포트 생성 호출
    try {
      if (appState.apiKey && appState.selectedTeam &&
          typeof homeLLMOutput !== "undefined" && homeLLMOutput) {
        const res = await fetch("/api/season-report", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            apiKey: appState.apiKey,
            user_team_id: appState.selectedTeam.id
          })
        });

        if (!res.ok) {
          const msg = await res.text();
          console.error("season-report API 에러:", msg);
          alert("시즌 결산 리포트를 생성하는 중 오류가 발생했습니다.");
        } else {
          const data = await res.json();
          const report =
            (data && (data.report_markdown || data.report)) || "";

          if (report) {
            // Home 탭 LLM 응답 박스에 표시
            homeLLMOutput.textContent = report;
            homeLLMOutput.classList.remove("muted");
            if (typeof handleSeasonReportGenerated === "function") {
              handleSeasonReportGenerated(report);
            }
          } else {
            homeLLMOutput.textContent =
              "시즌 결산 리포트를 생성하지 못했습니다.";
          }
        }
      } else {
        console.warn("apiKey 또는 selectedTeam이 없어 시즌 결산을 호출하지 못했습니다.");
      }
    } catch (e) {
      console.error("season-report 호출 중 예외:", e);
      alert("시즌 결산 리포트를 생성하는 중 오류가 발생했습니다. (콘솔 로그 참고)");
    }

    // 시뮬레이션 호출자는 더 이상 진행할 경기가 없음을 알 수 있어야 한다.
    return { success: false, reason: "no-more-regular-season" };
  }

  const homeTeam =
    TEAMS.find(t => t.id === nextGame.home_team_id) ||
    { id: nextGame.home_team_id, name: nextGame.home_team_id };
  const awayTeam =
    TEAMS.find(t => t.id === nextGame.away_team_id) ||
    { id: nextGame.away_team_id, name: nextGame.away_team_id };

  const gameDate = nextGame.date;

  // 🔹 1) 우리 팀 경기를 하기 전에, 다른 팀 경기들을 모두 그 날짜까지 자동 진행
  try {
    const resLeague = await fetch("/api/advance-league", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_date: gameDate,
        user_team_id: userTeam.id
      })
    });

    if (!resLeague.ok) {
      const msg = await resLeague.text();
      console.warn("advance-league 호출 실패:", msg);
    } else {
      const leagueData = await resLeague.json();
      const simulated = leagueData.simulated_games || [];

      simulated.forEach(g => {
        const hTeam =
          TEAMS.find(t => t.id === g.home_team_id) ||
          { id: g.home_team_id, name: g.home_team_id };
        const aTeam =
          TEAMS.find(t => t.id === g.away_team_id) ||
          { id: g.away_team_id, name: g.away_team_id };

        appState.cachedViews.scores.games.unshift({
          game_id: g.game_id,
          date: g.date,
          home_team_id: g.home_team_id,
          away_team_id: g.away_team_id,
          home_team_name: hTeam.name,
          away_team_name: aTeam.name,
          home_score: g.home_score,
          away_score: g.away_score,
          status: g.status || "final",
          is_overtime: g.is_overtime || false,
          top_performers: []
        });
      });

      if (simulated.length > 0) {
        appState.cachedViews.scores.latest_date = simulated[0].date;
      }
    }
  } catch (e) {
    console.error("advance-league 호출 중 오류:", e);
  }

  // 🔹 2) 피로도 계산
  const restDays = computeRestDaysForUserTeam();
  const fatigueFactor = calcFatigueFactor(restDays);

  const isUserHome = userTeam.id === homeTeam.id;
  const homeFatigue = isUserHome ? fatigueFactor : 1.0;
  const awayFatigue = !isUserHome ? fatigueFactor : 1.0;

  // 🔹 3) 우리 팀 경기 시뮬레이션 (/api/simulate-game)
  try {
    const homeTactics = buildTacticsForTeam(homeTeam.id, homeFatigue);
    const awayTactics = buildTacticsForTeam(awayTeam.id, awayFatigue);

    const res = await fetch("/api/simulate-game", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        home_team_id: homeTeam.id,
        away_team_id: awayTeam.id,
        home_tactics: homeTactics,
        away_tactics: awayTactics,
        game_date: gameDate
      })
    });

    if (!res.ok) {
      const msg = await res.text();
      alert("매치 엔진 호출 실패: " + msg);
      return false;
    }

    const data = await res.json();
    const finalScore = data.final_score || {};
    const homeScore = finalScore[homeTeam.id] ?? 0;
    const awayScore = finalScore[awayTeam.id] ?? 0;

    // 진행 턴 수 증가
    appState.progressTurns += 1;
    const turnId = `turn_${appState.progressTurns}`;
    appState.cachedViews.last_progress_turn_id = turnId;

    // Scores 탭 캐시 (유저 팀 경기)
    appState.cachedViews.scores.latest_date = gameDate;
    const userGameEntry = {
      game_id: nextGame.game_id,
      date: gameDate,
      home_team_id: homeTeam.id,
      away_team_id: awayTeam.id,
      home_team_name: homeTeam.name,
      away_team_name: awayTeam.name,
      home_score: homeScore,
      away_score: awayScore,
      status: "final",
      is_overtime: false,
      top_performers: []
    };

    appState.cachedViews.scores.games.unshift(userGameEntry);

    // 스케줄 항목에 결과 반영
    nextGame.home_score = homeScore;
    nextGame.away_score = awayScore;
    const myScore = isUserHome ? homeScore : awayScore;
    const oppScore = isUserHome ? awayScore : homeScore;
    nextGame.result_for_user_team = myScore > oppScore ? "W" : "L";

    // 뉴스 캐시
    const oppTeam = isUserHome ? awayTeam : homeTeam;
    appState.cachedViews.news.unshift({
      date: gameDate,
      title: `${userTeam.name}가 ${oppTeam.name}을(를) ${myScore}-${oppScore}로 ${
        myScore > oppScore ? "승리" : "패배"
      }`,
      summary: "파이썬 매치 엔진 결과를 기반으로 생성된 더미 뉴스입니다.",
      related_team_ids: [userTeam.id, oppTeam.id]
    });

    // 홈 로그 (홈 로그 영역이 있을 때만)
    if (typeof homeLog !== "undefined" && homeLog) {
      const logEntry = document.createElement("div");
      logEntry.className = "home-log-entry";
      logEntry.innerHTML = `<strong>${gameDate}</strong> · ${homeTeam.name} vs ${awayTeam.name} — ${homeScore}:${awayScore}`;
      homeLog.prepend(logEntry);
    }

    // LLM 해설(있다면) 홈 화면에 표시
    if (typeof homeLLMOutput !== "undefined" &&
        homeLLMOutput &&
        typeof data.commentary === "string") {
      homeLLMOutput.textContent = data.commentary;
    }

    // 진행 턴 수 라벨
    if (typeof progressLabel !== "undefined" && progressLabel) {
      progressLabel.textContent = `${appState.progressTurns}`;
    }

    // 인게임 날짜 업데이트
    appState.currentDate = gameDate;
    if (typeof seasonDateLabel !== "undefined" && seasonDateLabel) {
      seasonDateLabel.textContent = appState.currentDate;
    }

    // 탭들 다시 렌더
    renderAllTabs();
    renderSidebarRecentGames();

    // 스탯/순위/뉴스/팀 캐시 무효화
    const cv = appState.cachedViews;
    if (cv.stats) cv.stats.lastLoaded = null;
    if (cv.standings) cv.standings.lastLoaded = null;
    if (cv.weeklyNews) cv.weeklyNews.lastLoaded = null;
    if (cv.teams) cv.teams.lastLoaded = null;

    return {
      success: true,
      game_id: nextGame.game_id,
      game_date: gameDate,
      home_team_id: homeTeam.id,
      away_team_id: awayTeam.id,
      home_team_name: homeTeam.name,
      away_team_name: awayTeam.name,
      home_score: homeScore,
      away_score: awayScore,
      result_for_user_team: nextGame.result_for_user_team,
      log_entry: userGameEntry
    };
  } catch (err) {
    console.error(err);
    alert("매치 엔진 호출 중 오류가 발생했습니다. (콘솔 로그 확인)");
    return false;
  }
}
