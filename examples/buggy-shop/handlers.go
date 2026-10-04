package main

import (
	"encoding/json"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type api struct {
	store  *store
	config config
}

var emailPattern = regexp.MustCompile(`^[^\s@]+@[^\s@]+\.[^\s@]+$`)

type orderRequest struct {
	ProductID      *int     `json:"product_id"`
	Quantity       *int     `json:"quantity"`
	TotalAmount    *float64 `json:"total_amount"`
	DiscountAmount *float64 `json:"discount_amount"`
	StartTime      string   `json:"start_time"`
	EndTime        string   `json:"end_time"`
	PaymentMethod  string   `json:"payment_method"`
	CardToken      *string  `json:"card_token"`
}

func newHandler(data *store, cfg config) http.Handler {
	app := &api{store: data, config: cfg}
	mux := http.NewServeMux()
	mux.HandleFunc("/users", app.users)
	mux.HandleFunc("/users/", app.userByID)
	mux.HandleFunc("/orders", app.orders)
	mux.HandleFunc("/orders/", app.orderByID)
	if cfg.testMode {
		mux.HandleFunc("/test/reset", app.reset)
	}
	return mux
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	if body != nil {
		_ = json.NewEncoder(w).Encode(body)
	}
}

func decodeJSON(w http.ResponseWriter, r *http.Request, target any) bool {
	decoder := json.NewDecoder(r.Body)
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return false
	}
	return true
}

func resourceID(path, prefix string) (int, bool) {
	value := strings.TrimPrefix(path, prefix)
	if value == "" || strings.Contains(value, "/") {
		return 0, false
	}
	id, err := strconv.Atoi(value)
	return id, err == nil && id > 0
}

func (a *api) users(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var input user
	if !decodeJSON(w, r, &input) {
		return
	}
	validEmail := emailPattern.MatchString(input.Email)
	if input.Name == "" || input.Email == "" || (!a.config.acceptInvalidEmail && !validEmail) {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": "invalid user"})
		return
	}
	writeJSON(w, http.StatusCreated, a.store.createUser(input))
}

func (a *api) userByID(w http.ResponseWriter, r *http.Request) {
	id, ok := resourceID(r.URL.Path, "/users/")
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "user not found"})
		return
	}
	switch r.Method {
	case http.MethodGet:
		value, found := a.store.getUser(id)
		if !found {
			if a.config.unknownUserReturns200 {
				writeJSON(w, http.StatusOK, map[string]any{})
				return
			}
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "user not found"})
			return
		}
		writeJSON(w, http.StatusOK, value)
	case http.MethodDelete:
		if !a.store.deleteUser(id) {
			writeJSON(w, http.StatusNotFound, map[string]string{"error": "user not found"})
			return
		}
		w.WriteHeader(http.StatusNoContent)
	default:
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
	}
}

func (a *api) orders(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var input orderRequest
	if !decodeJSON(w, r, &input) {
		return
	}
	start, startErr := time.Parse(time.RFC3339, input.StartTime)
	end, endErr := time.Parse(time.RFC3339, input.EndTime)
	validPayment := input.PaymentMethod == "cash" || input.PaymentMethod == "card"
	validToken := (input.PaymentMethod == "cash" && input.CardToken == nil) ||
		(input.PaymentMethod == "card" && input.CardToken != nil && *input.CardToken != "")
	required := input.ProductID != nil && input.Quantity != nil &&
		input.TotalAmount != nil && input.DiscountAmount != nil
	invalid := !required || (required && (*input.ProductID < 1 || *input.Quantity < 1 || *input.Quantity > 100 ||
		*input.TotalAmount < 0 || *input.DiscountAmount < 0 || *input.DiscountAmount > *input.TotalAmount)) ||
		startErr != nil || endErr != nil || !end.After(start) || !validPayment || !validToken
	if invalid {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": "business rule violation"})
		return
	}
	value := order{
		ProductID: *input.ProductID, Quantity: *input.Quantity,
		TotalAmount: *input.TotalAmount, DiscountAmount: *input.DiscountAmount,
		StartTime: input.StartTime, EndTime: input.EndTime, PaymentMethod: input.PaymentMethod,
	}
	if input.CardToken != nil {
		value.CardToken = *input.CardToken
	}
	writeJSON(w, http.StatusCreated, a.store.createOrder(value))
}

func (a *api) orderByID(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	id, ok := resourceID(r.URL.Path, "/orders/")
	if !ok {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "order not found"})
		return
	}
	value, found := a.store.getOrder(id)
	if !found {
		writeJSON(w, http.StatusNotFound, map[string]string{"error": "order not found"})
		return
	}
	writeJSON(w, http.StatusOK, value)
}

func (a *api) reset(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	a.store.reset()
	w.WriteHeader(http.StatusNoContent)
}
