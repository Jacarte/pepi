package task

import (
	"fmt"
	"strings"
)

// ScoreMilestones returns the score and the maximum score. prefix counts the
// consecutive passes from the first milestone; count counts all passes.
func ScoreMilestones(scoring string, results []MilestoneResult) (score, maxScore int, err error) {
	maxScore = len(results)
	switch scoring {
	case ScoringPrefix:
		for _, r := range results {
			if !r.Passed {
				break
			}
			score++
		}
	case ScoringCount:
		for _, r := range results {
			if r.Passed {
				score++
			}
		}
	default:
		return 0, maxScore, fmt.Errorf("unknown scoring %q: want prefix or count", scoring)
	}
	return score, maxScore, nil
}

// Exit codes of scripts/check-milestone.sh and the timeout(1) wrapper around it.
const (
	checkMilestonePassed   = 0
	checkMilestoneFailed   = 1 // failed or rejected (cheat scan, restored tests)
	checkMilestoneBadUsage = 2
	checkMilestoneTimedOut = 124 // timeout(1) after SIGTERM
	checkMilestoneKilled   = 137 // timeout(1) after SIGKILL (-k)
)

// maxDetailBytes bounds MilestoneResult.Detail.
const maxDetailBytes = 300

// MilestoneFromCheck maps the exit code and output of check-milestone.sh to a result.
// Any code other than pass, fail or timeout is a harness problem and is an error.
func MilestoneFromCheck(m Milestone, exitCode int, output string) (MilestoneResult, error) {
	result := MilestoneResult{ID: m.ID, Title: m.Title}
	switch exitCode {
	case checkMilestonePassed:
		result.Passed = true
	case checkMilestoneFailed:
		result.Detail = failureLine(output)
	case checkMilestoneTimedOut, checkMilestoneKilled:
		result.Detail = "timed out"
	case checkMilestoneBadUsage:
		return result, fmt.Errorf("milestone %s: check-milestone.sh bad usage (exit 2): %s", m.ID, failureLine(output))
	default:
		return result, fmt.Errorf("milestone %s: check-milestone.sh unexpected exit %d: %s", m.ID, exitCode, failureLine(output))
	}
	return result, nil
}

// PrecheckDetail maps the exit code and output of the workspace precheck to the detail
// every milestone fails with. An unknown exit code is a harness error.
func PrecheckDetail(exitCode int, output string) (detail string, rejected bool, err error) {
	listed := strings.TrimSpace(output)
	switch exitCode {
	case 0:
		return "", false, nil
	case precheckSymlinks:
		return "workspace contains symlinks: " + listed, true, nil
	case precheckSpecial:
		return "workspace contains special files: " + listed, true, nil
	case precheckControl:
		return "workspace contains file names with control characters: " + listed, true, nil
	case precheckTooLarge:
		return fmt.Sprintf("workspace is too large: %s KiB (limit %d KiB)", listed, gradeMaxWorkspaceKiB), true, nil
	default:
		return "", false, fmt.Errorf("workspace precheck exit %d: %s", exitCode, failureLine(output))
	}
}

// failureLine is the last "REJECT:" line of the output, or the last non-empty line when
// there is none (for example a build error).
func failureLine(output string) string {
	last, reject := "", ""
	for _, line := range strings.Split(output, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		last = line
		if strings.HasPrefix(line, "REJECT:") {
			reject = line
		}
	}
	line := last
	if reject != "" {
		line = reject
	}
	if len(line) > maxDetailBytes {
		line = line[:maxDetailBytes] + "..."
	}
	return line
}

// FormatMilestoneLines renders one line per milestone ("m1-x PASS", "m2-y FAIL: detail")
// followed by the score line.
func FormatMilestoneLines(scoring string, results []MilestoneResult) ([]string, error) {
	score, maxScore, err := ScoreMilestones(scoring, results)
	if err != nil {
		return nil, err
	}
	lines := make([]string, 0, len(results)+1)
	for _, r := range results {
		if r.Passed {
			lines = append(lines, r.ID+" PASS")
		} else {
			lines = append(lines, r.ID+" FAIL: "+r.Detail)
		}
	}
	return append(lines, fmt.Sprintf("score %d/%d (%s)", score, maxScore, scoring)), nil
}
