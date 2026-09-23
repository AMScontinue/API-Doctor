package main

import (
	"encoding/json"
	"log"
	"net/http"
	"time"
)

type orderRequest struct {
	ProductID      int     `json:"product_id"`
	Quantity       int     `json:"quantity"`
	TotalAmount    float64 `json:"total_amount"`
	DiscountAmount float64 `json:"discount_amount"`
	StartTime      string  `json:"start_time"`
	EndTime        string  `json:"end_time"`
	PaymentMethod  string  `json:"payment_method"`
	CardToken      string  `json:"card_token"`
}

type userRequest struct {
	Name  string `json:"name"`
	Email string `json:"email"`
}

func writeJSON(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func createOrder(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var request orderRequest
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	start, startErr := time.Parse(time.RFC3339, request.StartTime)
	end, endErr := time.Parse(time.RFC3339, request.EndTime)
	invalid := request.ProductID < 1 || request.Quantity < 1 || request.Quantity > 100 ||
		request.TotalAmount < 0 || request.DiscountAmount < 0 || request.DiscountAmount > request.TotalAmount ||
		startErr != nil || endErr != nil || !end.After(start) ||
		(request.PaymentMethod == "card" && request.CardToken == "")
	if invalid {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": "business rule violation"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"id": 1, "status": "created"})
}

func createUser(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeJSON(w, http.StatusMethodNotAllowed, map[string]string{"error": "method not allowed"})
		return
	}
	var request userRequest
	if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "invalid JSON"})
		return
	}
	if request.Name == "" || request.Email == "" {
		writeJSON(w, http.StatusUnprocessableEntity, map[string]string{"error": "name and email are required"})
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"id": 1, "name": request.Name, "email": request.Email})
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("/orders", createOrder)
	mux.HandleFunc("/users", createUser)
	log.Print("buggy-shop spike service listening on 127.0.0.1:18080")
	log.Fatal(http.ListenAndServe("127.0.0.1:18080", mux))
}
