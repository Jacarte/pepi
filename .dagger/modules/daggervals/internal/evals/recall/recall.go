// Package recall measures how much Hippocampus recall injection adds to a Pi session. It reads
// the session transcript (Pi's JSONL), where every injected recall is a hippocampus-recall
// custom message, and checks that no memory is injected twice, that recall only happens on the
// turns allowed by recallEveryNTurns, and how many characters the injections add.
package recall

import (
	"bufio"
	"encoding/json"
	"fmt"
	"strings"
	"unicode/utf8"
)

// customType is the message type the Hippocampus extension injects on each recall.
const customType = "hippocampus-recall"

// MaxEveryNTurns is the largest recallEveryNTurns the extension accepts.
const MaxEveryNTurns = 50

// Report is what one transcript injected.
type Report struct {
	// Turns is the number of user prompts in the transcript.
	Turns int `json:"turns"`
	// RecallEveryNTurns is the gate the transcript was checked against.
	RecallEveryNTurns int `json:"recallEveryNTurns"`
	// Messages is the number of hippocampus-recall messages; at most ceil(turns/N).
	Messages int `json:"messages"`
	// InjectedIDs is the number of distinct memory ids injected; each appears once.
	InjectedIDs int `json:"injectedIds"`
	// InjectedChars is the total length of the injected message contents, in characters.
	InjectedChars int `json:"injectedChars"`
}

// entry is the part of a Pi transcript line that the checks read.
type entry struct {
	Type       string `json:"type"`
	CustomType string `json:"customType"`
	Message    struct {
		Role string `json:"role"`
	} `json:"message"`
	Content any `json:"content"`
	Details struct {
		Rows []struct {
			ID string `json:"id"`
		} `json:"rows"`
	} `json:"details"`
}

// Analyze checks a Pi session transcript against the recall gate. A turn is one user prompt;
// the recall of turn t is the message written after it. It fails on the first violation: a memory
// id injected again, two recalls on one turn, a recall on a turn the gate skips, or a recall before the first prompt.
func Analyze(jsonl string, recallEveryNTurns int) (Report, error) {
	if recallEveryNTurns < 1 || recallEveryNTurns > MaxEveryNTurns {
		return Report{}, fmt.Errorf("recallEveryNTurns must be 1 to %d, got %d", MaxEveryNTurns, recallEveryNTurns)
	}
	report := Report{RecallEveryNTurns: recallEveryNTurns}
	seen := map[string]bool{}
	recalledTurn := 0
	scanner := bufio.NewScanner(strings.NewReader(jsonl))
	scanner.Buffer(nil, 64<<20)
	for line := 1; scanner.Scan(); line++ {
		var e entry
		if err := json.Unmarshal(scanner.Bytes(), &e); err != nil {
			return Report{}, fmt.Errorf("transcript line %d is not JSON: %w", line, err)
		}
		if e.Type == "message" && e.Message.Role == "user" {
			report.Turns++
			continue
		}
		if e.Type != "custom_message" || e.CustomType != customType {
			continue
		}
		if report.Turns == 0 {
			return Report{}, fmt.Errorf("line %d: recall before the first prompt", line)
		}
		if (report.Turns-1)%recallEveryNTurns != 0 {
			return Report{}, fmt.Errorf("line %d: recall on turn %d, which the gate skips (every %d turns)", line, report.Turns, recallEveryNTurns)
		}
		if recalledTurn == report.Turns {
			return Report{}, fmt.Errorf("line %d: second recall on turn %d", line, report.Turns)
		}
		recalledTurn = report.Turns
		report.Messages++
		for _, row := range e.Details.Rows {
			if row.ID == "" {
				return Report{}, fmt.Errorf("line %d: recalled memory without an id", line)
			}
			if seen[row.ID] {
				return Report{}, fmt.Errorf("line %d: memory %s injected again", line, row.ID)
			}
			seen[row.ID] = true
		}
		content, ok := e.Content.(string)
		if !ok {
			return Report{}, fmt.Errorf("line %d: recall content is not text", line)
		}
		report.InjectedChars += utf8.RuneCountInString(content)
	}
	if err := scanner.Err(); err != nil {
		return Report{}, fmt.Errorf("read transcript: %w", err)
	}
	report.InjectedIDs = len(seen)
	return report, nil
}
