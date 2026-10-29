; Calls. @name: the called name; @receiver: what it is called on (Order in Order.find(id); a
; chained call; self); @arguments: the argument list, when there is one (render json: @order;
; not save!). A name alone (save, current_user) reads as a local variable or a call alike, and
; isn't captured.

; Order.new(...), OrderSerializer.new(@order): @name the class.
((call
   receiver: [(constant) (scope_resolution)] @name
   method: (identifier) @_new
   arguments: (argument_list)? @arguments) @call.new
 (#eq? @_new "new"))

((call
   receiver: (_) @receiver
   method: (identifier) @name
   arguments: (argument_list)? @arguments) @call.method
 (#not-eq? @name "new"))

(call
  !receiver
  method: (identifier) @name
  arguments: (argument_list)? @arguments) @call
