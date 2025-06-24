#include "hello.h"
#include <iostream>

Hello::Hello(const std::string& name) : m_name(name) {
}

void Hello::greet() const {
    std::cout << "Hello, " << m_name << "!" << std::endl;
}

const std::string& Hello::getName() const {
    return m_name;
}

int add_numbers(int a, int b) {
    return a + b;
}