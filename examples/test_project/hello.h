#pragma once
#include <string>

/**
 * A simple greeting class for testing
 */
class Hello {
public:
    /**
     * Constructor
     * @param name The name to greet
     */
    explicit Hello(const std::string& name);
    
    /**
     * Print a greeting message
     */
    void greet() const;
    
    /**
     * Get the name
     * @return The stored name
     */
    const std::string& getName() const;

private:
    std::string m_name;
};

/**
 * Add two numbers together
 * @param a First number
 * @param b Second number
 * @return Sum of a and b
 */
int add_numbers(int a, int b);