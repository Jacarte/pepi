package recall

import (
	"fmt"
	"strings"
	"testing"
)

// prompt is a user message line, as Pi writes it.
func prompt() string {
	return `{"type":"message","message":{"role":"user","content":[{"type":"text","text":"question"}]}}`
}

// recallLine is a hippocampus-recall custom message injecting the given memory ids.
func recallLine(content string, ids ...string) string {
	rows := make([]string, 0, len(ids))
	for _, id := range ids {
		rows = append(rows, fmt.Sprintf(`{"id":%q}`, id))
	}
	return fmt.Sprintf(`{"type":"custom_message","customType":"hippocampus-recall","content":%q,"details":{"rows":[%s]}}`, content, strings.Join(rows, ","))
}

// session writes prompts 1..turns. recallOn maps a prompt number to the recall line written after it.
func session(turns int, recallOn map[int]string) string {
	var lines []string
	for turn := 1; turn <= turns; turn++ {
		lines = append(lines, prompt())
		if line, ok := recallOn[turn]; ok {
			lines = append(lines, line)
		}
	}
	return strings.Join(lines, "\n")
}

func TestAnalyzeAcceptsRecallOnlyOnGatedTurns(t *testing.T) {
	jsonl := session(4, map[int]string{
		1: recallLine("Untrusted memory evidence\n1. id=m1", "m1"),
		4: recallLine("Untrusted memory evidence\n1. id=m2", "m2"),
	})
	report, err := Analyze(jsonl, 3)
	if err != nil {
		t.Fatal(err)
	}
	want := Report{Turns: 4, RecallEveryNTurns: 3, Messages: 2, InjectedIDs: 2, InjectedChars: len("Untrusted memory evidence\n1. id=m1") + len("Untrusted memory evidence\n1. id=m2")}
	if report != want {
		t.Fatalf("got %+v, want %+v", report, want)
	}
	if report.Messages > (report.Turns+2)/3 {
		t.Fatalf("messages %d above ceil(turns/N)", report.Messages)
	}
}

// Same memory on every turn: the dedup keeps one injection, which is within the bound.
func TestAnalyzeCountsOneInjectionForAMemoryKeptAcrossTurns(t *testing.T) {
	jsonl := session(5, map[int]string{1: recallLine("fact", "m1")})
	report, err := Analyze(jsonl, 1)
	if err != nil {
		t.Fatal(err)
	}
	if report.Turns != 5 || report.Messages != 1 || report.InjectedIDs != 1 || report.InjectedChars != len("fact") {
		t.Fatalf("got %+v", report)
	}
}

func TestAnalyzeRejectsMemoryInjectedTwice(t *testing.T) {
	jsonl := session(2, map[int]string{1: recallLine("fact", "m1"), 2: recallLine("fact again", "m1")})
	if _, err := Analyze(jsonl, 1); err == nil || !strings.Contains(err.Error(), "m1 injected again") {
		t.Fatalf("want a repeated-injection error, got %v", err)
	}
}

func TestAnalyzeRejectsRecallOnSkippedTurn(t *testing.T) {
	jsonl := session(3, map[int]string{2: recallLine("fact", "m1")})
	if _, err := Analyze(jsonl, 2); err == nil || !strings.Contains(err.Error(), "turn 2, which the gate skips") {
		t.Fatalf("want a skipped-turn error, got %v", err)
	}
}

func TestAnalyzeRejectsTwoRecallsOnOneTurn(t *testing.T) {
	jsonl := session(1, map[int]string{1: recallLine("a", "m1") + "\n" + recallLine("b", "m2")})
	if _, err := Analyze(jsonl, 1); err == nil || !strings.Contains(err.Error(), "second recall on turn 1") {
		t.Fatalf("want a second-recall error, got %v", err)
	}
}

func TestAnalyzeRejectsRecallBeforeAPrompt(t *testing.T) {
	if _, err := Analyze(recallLine("fact", "m1"), 1); err == nil {
		t.Fatal("want an error for a recall before the first prompt")
	}
}

func TestAnalyzeRejectsBadGateAndBadJSON(t *testing.T) {
	for _, n := range []int{0, MaxEveryNTurns + 1} {
		if _, err := Analyze("", n); err == nil {
			t.Fatalf("recallEveryNTurns %d: want an error", n)
		}
	}
	if _, err := Analyze("{", 1); err == nil {
		t.Fatal("want an error for a line that is not JSON")
	}
}

func TestAnalyzeIgnoresOtherEntries(t *testing.T) {
	jsonl := strings.Join([]string{
		`{"type":"session","id":"s"}`,
		prompt(),
		`{"type":"custom_message","customType":"subagent-notify","content":"done","details":{}}`,
		recallLine("bootstrap notice"),
	}, "\n")
	report, err := Analyze(jsonl, 1)
	if err != nil {
		t.Fatal(err)
	}
	if report.Turns != 1 || report.Messages != 1 || report.InjectedIDs != 0 {
		t.Fatalf("got %+v", report)
	}
}
