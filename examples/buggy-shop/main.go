package main

import (
	"log"
	"net/http"
	"os"
	"strings"
)

type config struct {
	address               string
	testMode              bool
	acceptInvalidEmail    bool
	unknownUserReturns200 bool
}

func loadConfig() config {
	defects := map[string]bool{}
	for _, name := range strings.Split(os.Getenv("BUGGY_SHOP_DEFECTS"), ",") {
		defects[strings.TrimSpace(name)] = true
	}
	address := os.Getenv("BUGGY_SHOP_ADDR")
	if address == "" {
		address = "127.0.0.1:18080"
	}
	return config{
		address:               address,
		testMode:              os.Getenv("BUGGY_SHOP_TEST_MODE") == "1",
		acceptInvalidEmail:    defects["invalid-email-accepted"],
		unknownUserReturns200: defects["unknown-user-200"],
	}
}

func main() {
	cfg := loadConfig()
	log.Printf("buggy-shop listening on %s", cfg.address)
	log.Fatal(http.ListenAndServe(cfg.address, newHandler(newStore(), cfg)))
}
