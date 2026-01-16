// 상태 관리 객체
let appState = {
  apiKey: null,
  selectedTeam: null,
  progressTurns: 0,
  // 인게임 날짜
  currentDate: null,
  cachedViews: {
    last_progress_turn_id: null,
    scores: { latest_date: null, games: [] },
    // past_games 제거 -> 전체 시즌 games + currentIndex
    schedule: { teamId: null, games: [], currentIndex: 0 },
    news: [],
    stats: { leaders: null, lastLoaded: null },
    standings: { east: [], west: [], lastLoaded: null },
    teams: { list: [], detailById: {}, lastLoaded: null },
    weeklyNews: { items: [], lastLoaded: null }
  },
  rosters: {},
  // ✅ LLM 대화 히스토리 (최근 N턴만 컨텍스트에 사용)
  chatHistory: [],
  // 어떤 팀의 퍼스트 메시지를 이미 보여줬는지 기록
  firstMessageShownTeams: {},
  // 팀별 전술 상태 저장
  tacticsByTeam: {},
  // 포스트시즌 진행 상태
  postseason: null,
  // 시즌 결산 생성 여부
  seasonReportReady: false
};

// 간단한 팀 데이터 (프런트 전용 메타)
const TEAMS = [
  { id: 'ATL', name: 'Atlanta Hawks', cap: '중간 시장', overall: 80, difficulty: '보통' },
  { id: 'BOS', name: 'Boston Celtics', cap: '우승 코어', overall: 95, difficulty: '쉬움' },
  { id: 'BKN', name: 'Brooklyn Nets', cap: '리툴 중', overall: 79, difficulty: '보통' },
  { id: 'CHA', name: 'Charlotte Hornets', cap: '재건', overall: 73, difficulty: '어려움' },
  { id: 'CHI', name: 'Chicago Bulls', cap: '애매한 전력', overall: 79, difficulty: '보통' },
  { id: 'CLE', name: 'Cleveland Cavaliers', cap: '젊은 코어', overall: 84, difficulty: '보통' },
  { id: 'DAL', name: 'Dallas Mavericks', cap: '슈퍼스타 1명', overall: 86, difficulty: '보통' },
  { id: 'DEN', name: 'Denver Nuggets', cap: '디펜딩 챔피언', overall: 94, difficulty: '쉬움' },
  { id: 'DET', name: 'Detroit Pistons', cap: '픽/유망주 풍부', overall: 72, difficulty: '매우 어려움' },
  { id: 'GSW', name: 'Golden State Warriors', cap: '사치세 한계', overall: 87, difficulty: '어려움' },
  { id: 'HOU', name: 'Houston Rockets', cap: '재건 막바지', overall: 78, difficulty: '보통' },
  { id: 'IND', name: 'Indiana Pacers', cap: '유연', overall: 81, difficulty: '보통' },
  { id: 'LAC', name: 'LA Clippers', cap: '사치세 심각', overall: 86, difficulty: '어려움' },
  { id: 'LAL', name: 'Los Angeles Lakers', cap: '스타 2명', overall: 89, difficulty: '보통' },
  { id: 'MEM', name: 'Memphis Grizzlies', cap: '젊은 코어', overall: 84, difficulty: '보통' },
  { id: 'MIA', name: 'Miami Heat', cap: '우승 지향', overall: 88, difficulty: '보통' },
  { id: 'MIL', name: 'Milwaukee Bucks', cap: '슈퍼맥스 2명', overall: 93, difficulty: '쉬움' },
  { id: 'MIN', name: 'Minnesota Timberwolves', cap: '빅맨 코어', overall: 85, difficulty: '보통' },
  { id: 'NOP', name: 'New Orleans Pelicans', cap: '유망주 다수', overall: 82, difficulty: '보통' },
  { id: 'NYK', name: 'New York Knicks', cap: '대시장', overall: 84, difficulty: '보통' },
  { id: 'OKC', name: 'Oklahoma City Thunder', cap: '픽 창고', overall: 83, difficulty: '보통' },
  { id: 'ORL', name: 'Orlando Magic', cap: '유망주 위주', overall: 79, difficulty: '어려움' },
  { id: 'PHI', name: 'Philadelphia 76ers', cap: 'MVP급 스타', overall: 90, difficulty: '보통' },
  { id: 'PHX', name: 'Phoenix Suns', cap: '슈퍼팀', overall: 92, difficulty: '쉬움' },
  { id: 'POR', name: 'Portland Trail Blazers', cap: '재건 시작', overall: 75, difficulty: '어려움' },
  { id: 'SAC', name: 'Sacramento Kings', cap: '플옵권', overall: 84, difficulty: '보통' },
  { id: 'SAS', name: 'San Antonio Spurs', cap: '유망주 에이스', overall: 78, difficulty: '어려움' },
  { id: 'TOR', name: 'Toronto Raptors', cap: '리툴 중', overall: 80, difficulty: '보통' },
  { id: 'UTA', name: 'Utah Jazz', cap: '픽 다수', overall: 77, difficulty: '어려움' },
  { id: 'WAS', name: 'Washington Wizards', cap: '재건', overall: 71, difficulty: '매우 어려움' }
];

// 팀 선택 화면(우측 상세 패널)에 표시할 추가 정보 (여기 내용을 팀별로 직접 채우세요)
// - key는 teamId (예: 'ATL')
// - ui.js가 아래 필드를 사용합니다:
//   home, offense, defense, corePlayers, picks, playStyle, cap, difficulty
//   (cap/difficulty는 TEAMS 값을 그대로 쓰려면 생략 가능)
const TEAM_SELECT_DETAILS = {
ATL: {
   home: 'Atlanta / State Farm Arena',
   offense: 3.5            
  // defense: 3.5                 
  // corePlayers: ['Jalen Johnson', 'Trae Young'],    
  // picks: '2025 1R, 2026 1R',  
  // playStyle: '빠른 템포, PnR 중심',
  // // cap: '중간 시장',
  // // difficulty: '보통',
},

BOS: {
  home: 'Boston / TD Garden',
  offense: 4.5,
  defense: 3,
  corePlayers: ['Jayson Tatum', 'Jaylen Brown'],
  picks: '2025 1R, 2026 1R',
  playStyle: '볼 무브먼트 중심',
},

BKN: {
  home: 'Brooklyn / Barclays Center',
  offense: 2,
  defense: 2.5,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '아이솔 + 스페이싱',
},

CHA: {
  home: 'Charlotte / Spectrum Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '빠른 트랜지션',
},

CHI: {
  home: 'Chicago / United Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '미드레인지 중심',
},

CLE: {
  home: 'Cleveland / Rocket Mortgage FieldHouse',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '수비 기반 하프코트',
},

DAL: {
  home: 'Dallas / American Airlines Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '볼 핸들러 중심',
},

DEN: {
  home: 'Denver / Ball Arena',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '하이포스트 플레이메이킹',
},

DET: {
  home: 'Detroit / Little Caesars Arena',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '젊은 코어 육성',
},

GSW: {
  home: 'Golden State / Chase Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '오프볼 무브먼트',
},

HOU: {
  home: 'Houston / Toyota Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '드라이브 앤 킥',
},

IND: {
  home: 'Indiana / Gainbridge Fieldhouse',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '빠른 템포',
},

LAC: {
  home: 'LA Clippers / Intuit Dome',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '윙 중심 아이솔',
},

LAL: {
  home: 'LA Lakers / Crypto.com Arena',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '스타 파워 중심',
},

MEM: {
  home: 'Memphis / FedExForum',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '공격적 수비 + 속공',
},

MIA: {
  home: 'Miami / Kaseya Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '하프코트 전술',
},

MIL: {
  home: 'Milwaukee / Fiserv Forum',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '림 어택 중심',
},

MIN: {
  home: 'Minnesota / Target Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '수비 + 리바운드',
},

NOP: {
  home: 'New Orleans / Smoothie King Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '인사이드 중심',
},

NYK: {
  home: 'New York / Madison Square Garden',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '피지컬 하프코트',
},

OKC: {
  home: 'Oklahoma City / Paycom Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '드라이브 기반',
},

ORL: {
  home: 'Orlando / Kia Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '수비 중심 성장',
},

PHI: {
  home: 'Philadelphia / Wells Fargo Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '포스트업 중심',
},

PHX: {
  home: 'Phoenix / Footprint Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '미드레인지 엘리트',
},

POR: {
  home: 'Portland / Moda Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '리빌딩',
},

SAC: {
  home: 'Sacramento / Golden 1 Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '빠른 볼 무브먼트',
},

SAS: {
  home: 'San Antonio / Frost Bank Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '기본기 중심',
},

TOR: {
  home: 'Toronto / Scotiabank Arena',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '스위치 수비',
},

UTA: {
  home: 'Utah / Delta Center',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '조직적인 하프코트',
},

WAS: {
  home: 'Washington / Capital One Arena',
  offense: 3,
  defense: 2,
  corePlayers: ['A', 'B'],
  picks: '2025 1R, 2026 1R',
  playStyle: '리빌딩',
},

};


const DIVISIONS = {
  West: {
    Southwest: ['DAL', 'HOU', 'MEM', 'NOP', 'SAS'],
    Northwest: ['DEN', 'MIN', 'OKC', 'POR', 'UTA'],
    Pacific: ['GSW', 'LAC', 'LAL', 'PHX', 'SAC']
  },
  East: {
    Atlantic: ['BOS', 'BKN', 'NYK', 'PHI', 'TOR'],
    Central: ['CHI', 'CLE', 'DET', 'IND', 'MIL'],
    Southeast: ['ATL', 'CHA', 'MIA', 'ORL', 'WAS']
  }
};

function getDefaultTactics() {
  return {
    pace: 0,
    offenseScheme: 'pace_space',
    offenseSecondaryScheme: 'pace_space',
    offensePrimaryWeight: 5,
    offenseSecondaryWeight: 5,
    defenseScheme: 'drop_coverage',
    defenseSecondaryScheme: 'drop_coverage',
    defensePrimaryWeight: 5,
    defenseSecondaryWeight: 5,
    rotationSize: 9,
    starters: [],
    bench: [],
    minutes: {}
  };
}

function getOrCreateTacticsForTeam(teamId) {
  if (!teamId) return null;
  if (!appState.tacticsByTeam) {
    appState.tacticsByTeam = {};
  }
  if (!appState.tacticsByTeam[teamId]) {
    appState.tacticsByTeam[teamId] = getDefaultTactics();
  }
  return appState.tacticsByTeam[teamId];
}

function getTeamConfAndDiv(teamId) {
  for (const [conf, divs] of Object.entries(DIVISIONS)) {
    for (const [divName, teamArr] of Object.entries(divs)) {
      if (teamArr.includes(teamId)) {
        return { conference: conf, division: divName };
      }
    }
  }
  return { conference: null, division: null };
}
