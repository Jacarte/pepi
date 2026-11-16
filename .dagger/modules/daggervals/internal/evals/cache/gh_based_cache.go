package cache

import (
	"context"
	"dagger/daggervals/internal/dagger"
	"dagger/daggervals/internal/evals/engine"
	"encoding/json"
	"sync"
)

type GHCache struct {
	dirLock   sync.RWMutex
	directory *dagger.Directory
	context   context.Context
}

func NewGHCache(hostDirectory *dagger.Directory, ctx context.Context) *GHCache {
	return &GHCache{
		directory: hostDirectory,
		context:   ctx,
	}
}

func (c *GHCache) Get(runId string) ([]engine.TaskOutcome, error) {

	// Check if file exists first
	exists, err := c.directory.Exists(c.context, runId)
	if err != nil {
		return nil, err
	}
	if !exists {
		// return empty
		return []engine.TaskOutcome{}, nil // no cache for this runId
	}
	c.dirLock.RLock()
	defer c.dirLock.RUnlock()
	cacheVal, err := c.directory.File(runId).Contents(c.context)

	if err != nil {
		return nil, err
	}
	var outcomes []engine.TaskOutcome
	err = json.Unmarshal([]byte(cacheVal), &outcomes)
	if err != nil {
		return nil, err
	}
	return outcomes, nil

}

func (c *GHCache) Set(runId string, outcomes []engine.TaskOutcome) error {
	content, err := json.Marshal(outcomes)
	if err != nil {
		return err
	}
	c.dirLock.Lock()
	defer c.dirLock.Unlock()

	c.directory = c.directory.WithNewFile(runId, string(content))
	return nil
}

func (c *GHCache) GetDirectory() *dagger.Directory {
	return c.directory
}
