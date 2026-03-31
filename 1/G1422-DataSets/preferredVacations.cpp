//============================================================================
// Name        : preferredVacation.cpp
// Authors     : Atoosa Kasirzadeh & Frederic Quesnel
// Version     : 1.0
// Description : Vacation Preference Random Generator
//				 Assign random vacation dates for fictive employees for multiple instances at once.
//				 For one instance, the number of employees selected correspond to a certain percentage of all employees.
//				 The number of employees and their respective bases are defined in the file "listOfBases.csv", placed in each instance file.
//				 The vacations occur from 2002/10/01 to 2002/10/31, with a length between 2 and 15 days. They all start at 00:00 and end at 23:59.
//
//				 This program outputs a list of all the employees to wich are assigned vacations, with their respective bases, vacation start and vacation end.
//               The output is in csv format.
//
//				 arguments : path/to/parameters/file
//
//				 For more details on how to use this program, see the README file.
//============================================================================

#include <iostream>
#include <fstream>
#include <sstream>
#include <cstdlib>
#include <ctime>
#include <vector>


using std::cout;
using std::cin;
using std::ios;
using std::endl;
using std::string;
using std::vector;

int main(int argc, char* argv[]) {

	//seed of random number generator
	srand((unsigned)time(0));

	//percentage of airlegs to be choosen
	float percentageOfChosen;

	//list of the names of the instances to be processed
	vector<string> instanceNames;

	string a;//dummy variable used to discard useless input

	//---------------------------------------------
	//extracts arguments from the parameter file

	//name of parameter file
	std::stringstream paramsName(argv[1]);
	//open the parameter file
	std::ifstream params(paramsName.str().c_str());

	//string used to contain the lines of the parameter file
	string paramLine ;

	//skip lines until it arrives at the "instances" line
	while(getline(params,paramLine) && !paramLine.compare("instances")==0){
	}

	//extract all instance names, until an empty line is reached
	while(getline(params,paramLine) && !paramLine.compare("")==0){
		instanceNames.push_back(paramLine);
	}

	//skip lines until it arrives at the "preferredVacations.cpp" line
	while(getline(params,paramLine) && !paramLine.compare("preferredVacations.cpp")==0){
	}
	//extract the percentage of employees chosen
	params >> a>>a>>a>>a>>percentageOfChosen;

	//number of current instance
	int instanceNb = 0;

	//----------------------------------------------
	//generate vacations for each instance

	while(instanceNb < (int) instanceNames.size()){

		//name of output file
		std::stringstream outName;
		outName<<instanceNames[instanceNb]<<"/personalizedEmployees.csv";

		//name of input file (listOfBases.csv)
		std::stringstream inName;
		inName <<instanceNames[instanceNb]<<"/listOfBases.csv";

		//opens the input file, containing the list of bases
		std::ifstream bases;
		bases.open(inName.str().c_str(),ios::in);

		//if listOfBases.csv, displays an error message and proceed with the next instance
		if(bases.fail()){
			cout<<"Error : could not open instance folder : "<<instanceNames[instanceNb]<<".  Proceeding with next instance..."<<endl;
		}
		//the file "listOfBases.csv" is open
		else{

			//creates the output file
			std::ofstream fileout(outName.str().c_str(),ios::out | ios::trunc);
			//if the program fails to open the output file, display an error message and proceed with next instance
			if(fileout.fail()){
				cout << "Error : could not open output file personalizedEmployees.csv in instance "<<instanceNames[instanceNb]<<". Proceeding with next instance."<<endl;
			}
			else{
				//first line of the file (header)
				fileout << "employee , base , vacationName , startDate , startHour , endDate , endHour"<< endl;

				//-----------------------------------------------
				//Extracts information from listOfBases :

				//  - count the number of employees
				//  - lists the names of the airports
				//  - lists whether an airport is a base or not
				//  - counts the number of employees for each airport

				//total number of employees
				int noEmp = 0;

				//name of airports
				vector<string> airports;
				//determine if the airport is a base (contains employees)
				vector<bool> isBase;
				//number of employees for each airport/base
				vector<int> nbEmp;

				//one line of the input file
				string line;

				//first line is the header and is discarded
				getline(bases,a);

				string strAirport;
				bool boolIsBase;
				int NEmployees;
				
				//for each line of the file
				while(bases>>strAirport>>a>>boolIsBase>>a>>NEmployees){
					
					//extract name of airport
					airports.push_back(strAirport.c_str());

					//remove the airport name from the variable "line"
					line = line.substr(line.find_first_of(",")+1);

					//extracts whether the airport is a base
					isBase.push_back(boolIsBase);
					//remove the information from the variable "line"
					line = line.substr(line.find_first_of(",")+1);

					//extract the number of employees
					nbEmp.push_back(NEmployees);

					//add the number of employees to the total number of employees
					noEmp += NEmployees;
				}

				//--------------------------------------------
				// assign a base to each employee, considering the number of employees for each base

				//array containing the base of each employee
				vector<string> basesOfEmployees;

				//for each base
				for(int i=0;i< (int) isBase.size();i++){

					//if the airport is a base
					if(isBase[i]){
						string baseName = airports[i];
						int empAtBase = nbEmp[i];
						//assign the right number of employees to the base
						for(int j=0;j<empAtBase;j++){
							basesOfEmployees.push_back(baseName);
						}
					}
				}


				//--------------------------------------------------------
				// randomly select employees and their vacations and write theme in the output file

				//list of employees with preferred vacations
				vector <int> EmpWithPref;

				//choose randomly "percentageOfChoosen" % of all employees
				for (int i=1 ; i<=noEmp*percentageOfChosen/100; i++ ){

					int random_integer = rand();
					//random number representing the index in "basesOfEmployees" where the employee is chosen
					int baseIndex  = random_integer % basesOfEmployees.size();

					random_integer = rand();
					//duration of vacation ( 2-15 days)
					int vacDur  = random_integer %(13)+2 ;
					//select the starting day of the vacation
					int randomStart_integer = rand();
					int startDay  = randomStart_integer %(31-vacDur+1)+1;

					//writes vacation to the output file
					//employee identification
					if (i<=9){
						fileout << "EMP00" << i  << " , "<< basesOfEmployees[baseIndex]<< " , Vacation_00"<<i  ;
					}
					else if (i<=99){
						fileout << "EMP0" << i   << " , "<< basesOfEmployees[baseIndex] << " , Vacation_0"<<i  ;
					}
					else{
						fileout << "EMP" << i    << " , "<< basesOfEmployees[baseIndex] << " , Vacation_"  <<i ;
					}

					//start day (if <10, the 0 preceding the day in the date must be added manually)
					if(startDay>= 10){
						fileout << " , 2000-01-" <<startDay << " , 00:00";
					}
					else{
						fileout << " , 2000-01-0" <<startDay << " , 00:00";
					}
					//end day (if <10, the 0 preceding the day in the date must be added manually)
					if(startDay + vacDur >= 10){
						fileout <<  " , 2000-01-"<< startDay + vacDur <<" , 23:59" << endl;
					}
					else{
						fileout <<  " , 2000-01-0"<< startDay + vacDur <<" , 23:59" << endl;
					}

					//remove employee so he is not choosen twice
					basesOfEmployees.erase(basesOfEmployees.begin() + baseIndex);
				}
				//close output file
				fileout.close();
			}
		}
		//close input file
		bases.close();

		//next instance
		instanceNb++;
	}
	return(0);
}
