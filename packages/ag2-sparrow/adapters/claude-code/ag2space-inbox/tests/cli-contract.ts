// The inbox CLI's answers, scenario by scenario: tests/test_claude_code_adapter.py runs each against the real
// verbs, and the harness runs each against its fake, so the fake cannot answer what the CLI does not.
export const CONTRACT = [
 {
  "name": "claim an absent owner task",
  "task": "task-c1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   }
  ]
 },
 {
  "name": "claim a task whose tier is not admitted: answered once",
  "task": "task-c2",
  "tier": "guest",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c2"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   },
   {
    "argv": [
     "claim",
     "task-c2"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "claim a task with a ready result",
  "task": "task-c3",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c3"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ],
  "result": "an answer\n"
 },
 {
  "name": "claim a task with an unready result",
  "task": "task-c4",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c4"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": true
    }
   }
  ],
  "result": "  \n"
 },
 {
  "name": "claim a prepared task again",
  "task": "task-c5",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c5"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "claim",
     "task-c5"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   }
  ]
 },
 {
  "name": "claim a calling task",
  "task": "task-c6",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c6"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-c6",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "claim",
     "task-c6"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "claim after an unconfirmed turn",
  "task": "task-c7",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-c7"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-c7",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-c7",
     "turn-1",
     "bad",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   },
   {
    "argv": [
     "claim",
     "task-c7"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "authorize the same request twice",
  "task": "task-a1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-a1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-a1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-a1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   }
  ]
 },
 {
  "name": "authorize an unclaimed task",
  "task": "task-a2",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "authorize",
     "task-a2",
     "1",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "authorize another text after the first",
  "task": "task-a3",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-a3"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-a3",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-a3",
     "{attempt}",
     "--text-hash",
     "cdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcdcd"
    ],
    "save": "token",
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "confirm the authorized turn",
  "task": "task-f1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-f1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-f1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-f1",
     "turn-1",
     "{token}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "turn_id": "turn-1"
     }
    }
   }
  ]
 },
 {
  "name": "stage on another turn, then this turn, then another body",
  "task": "task-s1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-s1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-s1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-s1",
     "turn-1",
     "{token}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "turn_id": "turn-1"
     }
    }
   },
   {
    "argv": [
     "stage",
     "task-s1",
     "turn-2"
    ],
    "stdin": "x\n",
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   },
   {
    "argv": [
     "stage",
     "task-s1",
     "turn-1"
    ],
    "stdin": "ans\n",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "outcome": "staged"
     }
    }
   },
   {
    "argv": [
     "stage",
     "task-s1",
     "turn-1"
    ],
    "stdin": "other\n",
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   },
   {
    "argv": [
     "publish",
     "task-s1"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "outcome": "settled"
     }
    }
   },
   {
    "argv": [
     "claim",
     "task-s1"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "publish with nothing staged",
  "task": "task-p1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-p1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-p1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-p1",
     "turn-1",
     "{token}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "turn_id": "turn-1"
     }
    }
   },
   {
    "argv": [
     "publish",
     "task-p1"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "park on another turn, then this turn",
  "task": "task-k1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-k1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt",
      "admission",
      "access_tier"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-k1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-k1",
     "turn-1",
     "{token}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "turn_id": "turn-1"
     }
    }
   },
   {
    "argv": [
     "park",
     "task-k1",
     "turn-2",
     "--detail",
     "turn completed without a reply"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   },
   {
    "argv": [
     "park",
     "task-k1",
     "turn-1",
     "--detail",
     "turn completed without a reply"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "outcome": "settled",
      "parked": "turn completed without a reply"
     }
    }
   }
  ]
 },
 {
  "name": "interrupt an absent task, then again",
  "task": "task-i1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "interrupt",
     "task-i1",
     "--detail",
     "stopped"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "resolved": true,
      "answered": true,
      "interrupted": true
     }
    }
   },
   {
    "argv": [
     "interrupt",
     "task-i1",
     "--detail",
     "stopped"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "resolved": true,
      "answered": false
     }
    }
   }
  ]
 },
 {
  "name": "interrupt a task with an unready result",
  "task": "task-i2",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "interrupt",
     "task-i2",
     "--detail",
     "stopped"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": true
    }
   }
  ],
  "result": "  \n"
 },
 {
  "name": "interrupt a task with a ready result",
  "task": "task-i3",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "interrupt",
     "task-i3",
     "--detail",
     "stopped"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "resolved": true,
      "answered": false
     }
    }
   }
  ],
  "result": "an answer\n"
 },
 {
  "name": "mark a task stuck, then clear it",
  "task": "task-m1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "progress",
     "--stuck",
     "task-m1",
     "--stuck-detail",
     "held"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "stuck"
     ]
    }
   },
   {
    "argv": [
     "progress",
     "--unstuck",
     "task-m1"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false
    }
   }
  ]
 },
 {
  "name": "mark an unconfirmed (calling) task stuck: kept",
  "task": "task-q1",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-q1"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-q1",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-q1",
     "turn-1",
     "bad",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 1,
     "refused": true,
     "retry": false
    }
   },
   {
    "argv": [
     "progress",
     "--stuck",
     "task-q1",
     "--stuck-detail",
     "held"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "marked": [
      "task-q1"
     ]
    }
   }
  ]
 },
 {
  "name": "mark a task with a ready result stuck: dropped as resolved",
  "task": "task-q2",
  "tier": "owner",
  "result": "an answer\n",
  "steps": [
   {
    "argv": [
     "progress",
     "--stuck",
     "task-q2",
     "--stuck-detail",
     "held"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "unmarked": [
      "task-q2"
     ]
    }
   }
  ]
 },
 {
  "name": "mark a task with an archived result stuck: dropped as resolved",
  "task": "task-q3",
  "tier": "owner",
  "archived": "an answer\n",
  "steps": [
   {
    "argv": [
     "progress",
     "--stuck",
     "task-q3",
     "--stuck-detail",
     "held"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "unmarked": [
      "task-q3"
     ]
    }
   }
  ]
 },
 {
  "name": "mark a published task stuck: dropped as resolved",
  "task": "task-q4",
  "tier": "owner",
  "steps": [
   {
    "argv": [
     "claim",
     "task-q4"
    ],
    "save": "attempt",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "attempt"
     ]
    }
   },
   {
    "argv": [
     "authorize",
     "task-q4",
     "{attempt}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "save": "token",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "has": [
      "token"
     ]
    }
   },
   {
    "argv": [
     "confirm",
     "task-q4",
     "turn-1",
     "{token}",
     "--text-hash",
     "abababababababababababababababababababababababababababababababab"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false
    }
   },
   {
    "argv": [
     "stage",
     "task-q4",
     "turn-1"
    ],
    "stdin": "ans\n",
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "outcome": "staged"
     }
    }
   },
   {
    "argv": [
     "publish",
     "task-q4"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "fields": {
      "outcome": "settled"
     }
    }
   },
   {
    "argv": [
     "progress",
     "--stuck",
     "task-q4",
     "--stuck-detail",
     "held"
    ],
    "expect": {
     "exit": 0,
     "refused": false,
     "retry": false,
     "unmarked": [
      "task-q4"
     ]
    }
   }
  ]
 }
]
