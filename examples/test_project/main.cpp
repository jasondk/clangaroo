#include "hello.h"
#include <iostream>

int main() {
    std::cout << "Starting test..." << std::endl;
    
    // Test class instantiation
    Hello hello("World");
    hello.greet();
    
    // Test function call
    int result = add_numbers(5, 3);
    std::cout << "Result: " << result << std::endl;
    
    return 0;
}